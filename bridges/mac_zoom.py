"""Magnified-tap confirmation for the macbook view (runs on the MacBook).

Two-stage tap support next to ``bridges/macos_state.py`` (same stdlib
shape, PyObjC and subprocesses isolated in small functions so the pure
math stays importable anywhere):

- After the bridge warps the cursor (stage 1: move ONLY, never click),
  :func:`capture_around` grabs a small crop around the new position
  with ``screencapture`` and the bridge POSTs it to
  ``/feed/macbook/zoom`` as bounded base64 JPEG. The panel blows it up
  as the review surface: what is under the cursor before stage 2.
- Stage 2 arrives as a click command (``GET /macbook/click?since=``);
  :func:`do_click` posts one left down+up pair at that point.

Bounds (deliberate): the crop is CROP_W x CROP_H Quartz points and the
JPEG byte cap is JPEG_CAP, so the wire document can never exceed ~4/3
of that -- the daemon schema enforces the same ceiling. A stale or
missing capture is a reason to show NOTHING (the renderer draws a hint
instead), never an old screenshot presented as current.
"""

import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.request

CROP_W, CROP_H = 480, 360
JPEG_CAP = 98304  # bytes: wire stays under ~128 KiB base64
CLICK_TTL = 15.0  # mirrors the daemon slot: stale taps never fire


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


def capture(crop, runner=None):
    """Screenshot one crop rect -> JPEG bytes (bounded by JPEG_CAP).

    `runner` injects the subprocess call for tests. Raises on any
    failure (screen recording denied, tool missing): the caller logs
    and posts nothing, so no review surface appears without a fresh
    capture behind it."""
    x, y, w, h = (int(crop["x"]), int(crop["y"]),
                  int(crop["w"]), int(crop["h"]))
    if w <= 0 or h <= 0 or w * h > 4 * CROP_W * CROP_H:
        raise ValueError("crop out of bounds: %r" % (crop,))
    fd, path = tempfile.mkstemp(prefix="displayd-zoom-", suffix=".jpg")
    os.close(fd)
    try:
        run = runner or subprocess.run
        proc = run(["/usr/sbin/screencapture", "-x", "-t", "jpg",
                    "-R%d,%d,%d,%d" % (x, y, w, h), path],
                   timeout=10, capture_output=True)
        if getattr(proc, "returncode", 1) != 0:
            raise RuntimeError("screencapture failed: %s" % (
                getattr(proc, "stderr", b"") or b"")[:160])
        with open(path, "rb") as fh:
            data = fh.read(JPEG_CAP + 1)
        if not data:
            raise RuntimeError("screencapture produced no image")
        if len(data) > JPEG_CAP:
            raise RuntimeError("capture %d bytes over cap %d"
                               % (len(data), JPEG_CAP))
        return data
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


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
    """Pending click command newer than `since`; None when idle.
    Best-effort like the mouse fetch: any failure means no click."""
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


def position_hook(displayd_base, state, x, y, now=None):
    """Stage-1 follow-through: capture around (x, y) and POST zoom.
    Returns True when a fresh capture was posted. Never raises: any
    failure posts nothing, so the panel shows no misleading image."""
    try:
        now = time.time() if now is None else float(now)
        displays = (state.get("displays") if isinstance(state, dict)
                    else None) or []
        crop = crop_for(x, y, displays)
        if crop is None:
            return False
        text = encode(capture(crop))
        doc = zoom_doc(x, y, text, now)
        return bool(doc) and post_zoom(displayd_base, doc)
    except Exception:
        return False


def do_click(x, y):
    """One left click at Quartz (x, y): down+up posted atomically.

    Direct CGEventPost, NOT Talon (the existing Talon click path stays
    untouched; this is the panel's own second-tap commit). Raises on
    failure so the caller logs and skips -- a failed click clicks
    nothing, by design. Range-checked BEFORE the Quartz import so the
    refusal is testable (and safe) on machines without PyObjC."""
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
