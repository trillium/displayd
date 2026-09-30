"""Magnified-tap confirmation for the macbook view (runs on the MacBook).

After the bridge warps the cursor (stage 1: move ONLY), Talon itself
captures the crop -- ``screen.capture_rect`` over the file channel
(``bridges/talon_channel.py``) -- and the bridge POSTs bounded base64
JPEG to ``/feed/macbook/zoom`` for the review surface. Stage 2
(``GET /macbook/click?since=``) posts one CG down+up pair. When the
Talon capture fails, a bounded ffmpeg crop (same avfoundation pixel
path as the live-verified preview thread) fires instead, so one
broken capture mechanism can no longer starve the zoom feed stale.

Bounds: JPEG_CAP bounds the wire (~4/3, schema-enforced); Talon
writes one FIXED comm-dir PNG (no channel-supplied path, no
traversal), converted here via PIL. No fresh capture -> no image,
never a stale one; Talon down degrades to hint + stage-2 refusal
while the direct warp keeps working.
"""

import base64
import io
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import talon_channel as channel
except Exception:  # never: stdlib-only sibling, same directory
    channel = None

CROP_W, CROP_H = 480, 360
# File-byte cap: base64 of this many bytes is at most 133804 chars,
# under the daemon schema ceiling (140000) with margin to spare. The
# cap is on the WIRE size that matters, not an arbitrary round number.
JPEG_CAP = 100352
# PNG read cap: a 480x360 UI crop never approaches this; over is refused.
PNG_CAP = 524288
CAPTURE_IMAGE = "capture_image.png"
JPEG_QUALITY = 70
CLICK_TTL = 15.0  # mirrors the daemon slot: stale taps never fire

LOG = logging.getLogger("mac-zoom")


def _num(value):
    return value if isinstance(value, (int, float)) \
        and not isinstance(value, bool) else None


def crop_for(x, y, displays, w=CROP_W, h=CROP_H):
    """Crop rect around (x, y), clamped inside its display.

    Returns {"x","y","w","h"} ints, or None when the point is on no
    known display (capture nothing rather than the wrong screen).
    Pure; never raises."""
    try:
        qx, qy = float(x), float(y)
        w, h = int(w), int(h)
        if w <= 0 or h <= 0:
            return None
    except (TypeError, ValueError):
        return None
    box = None
    try:
        for d in displays or []:
            b = d.get("bounds") if isinstance(d, dict) else None
            if not isinstance(b, dict):
                continue
            bx, by = _num(b.get("x")), _num(b.get("y"))
            bw, bh = _num(b.get("w")), _num(b.get("h"))
            if None in (bx, by, bw, bh) or bw <= 0 or bh <= 0:
                continue
            if bx <= qx < bx + bw and by <= qy < by + bh:
                box = (bx, by, bw, bh)
                break
    except Exception:
        return None
    if box is None:
        return None
    bx, by, bw, bh = box
    cx = min(max(int(qx - w / 2), int(bx)), int(bx + bw - w))
    cy = min(max(int(qy - h / 2), int(by)), int(by + bh - h))
    if cx < bx or cy < by:  # display smaller than the crop: whole screen
        return {"x": int(bx), "y": int(by),
                "w": int(min(w, bw)), "h": int(min(h, bh))}
    return {"x": cx, "y": cy, "w": w, "h": h}


def comm_dir(path=None):
    """Talon channel directory (None when the channel is missing)."""
    try:
        return channel.comm_dir(path) if channel is not None else None
    except Exception:
        return None


def capture_id(ts=None):
    """Unique capture request id (bridge pid + time). Never raises."""
    try:
        return "%d-%d" % (os.getpid(), int((ts or time.time()) * 1e6))
    except Exception:
        return "capture-%d" % os.getpid()


def capture(directory, crop, timeout=4.0):
    """Screenshot one crop rect -> JPEG bytes (bounded by JPEG_CAP).

    Asks Talon through the file channel, converts its fixed-name PNG
    to JPEG here. Raises on any failure: the caller posts nothing."""
    x, y, w, h = (int(crop["x"]), int(crop["y"]),
                  int(crop["w"]), int(crop["h"]))
    if w <= 0 or h <= 0 or w * h > 4 * CROP_W * CROP_H:
        raise ValueError("crop out of bounds: %r" % (crop,))
    if channel is None:
        raise RuntimeError("talon channel unavailable")
    try:
        from PIL import Image
    except Exception:
        raise RuntimeError("PIL unavailable for PNG->JPEG")
    body = {"id": capture_id(), "x": x, "y": y, "w": w, "h": h,
            "ts": time.time()}
    resp = channel.exchange(directory, "capture_request.json",
                            "capture_response.json", body,
                            timeout=timeout)
    try:
        channel.cleanup(directory, "capture_response.json")
    except Exception:
        pass
    if not isinstance(resp, dict) or not resp.get("ok"):
        raise RuntimeError("talon capture refused: %r" % (resp,))
    if resp.get("id") != body["id"]:
        raise RuntimeError("talon capture id mismatch")
    path = os.path.join(directory, CAPTURE_IMAGE)
    try:
        with open(path, "rb") as fh:
            png = fh.read(PNG_CAP + 1)
    except Exception as err:
        raise RuntimeError("capture image unreadable: %s" % (err,))
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
    if not png or len(png) > PNG_CAP:
        raise RuntimeError("capture PNG missing or over cap")
    try:
        shot = Image.open(io.BytesIO(png))
        shot.load()
        out = io.BytesIO()
        shot.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY)
        data = out.getvalue()
    except Exception as err:
        raise RuntimeError("PNG->JPEG failed: %s" % (err,))
    if not data or len(data) > JPEG_CAP:
        raise RuntimeError("capture JPEG missing or over cap")
    return data


def encode(data, cap=JPEG_CAP):
    """JPEG bytes -> base64 text, or None when over cap. Never raises."""
    try:
        if not isinstance(data, (bytes, bytearray)) or not data:
            return None
        if len(data) > cap:
            return None
        return base64.b64encode(bytes(data)).decode("ascii")
    except Exception:
        return None


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


def zoom_doc(x, y, jpeg_text, now):
    """One /feed/macbook/zoom document. Never raises (None on garbage)."""
    try:
        if not jpeg_text or not isinstance(jpeg_text, str):
            return None
        return {"ts": float(now), "x": float(x), "y": float(y),
                "jpeg": jpeg_text}
    except (TypeError, ValueError):
        return None


def post_zoom(displayd_base, doc, timeout=5.0):
    """POST one zoom document to the panel feed; True on success."""
    url = displayd_base.rstrip("/") + "/feed/macbook/zoom"
    req = urllib.request.Request(url, data=json.dumps(doc).encode("utf-8"),
                                 headers={"Content-Type":
                                          "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1024)
    except Exception:
        return False
    return True


def fetch_click_command(displayd_base, since=0.0, timeout=2.0):
    """Pending click newer than `since`; None when idle or stale."""
    url = (displayd_base.rstrip("/") + "/macbook/click"
           + "?since=%s" % since)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            doc = json.loads(resp.read(4096).decode("utf-8", "replace"))
        cmd = doc.get("command") if isinstance(doc, dict) else None
        x, y, ts = float(cmd["x"]), float(cmd["y"]), float(cmd.get("ts", 0))
    except Exception:
        return None
    if abs(x) > 100000 or abs(y) > 100000:
        return None
    if time.time() - ts > CLICK_TTL:
        return None
    return {"x": x, "y": y, "ts": ts,
            "display_index": cmd.get("display_index")}


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
