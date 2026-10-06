"""The Talon file-channel capture behind the zoom review.

Single concept: asking Talon for one crop and turning its answer into the
bounded JPEG bytes the review feed carries -- the fixed comm-dir exchange,
the fixed-name PNG read (no channel-supplied path, no traversal), the
PNG->JPEG conversion, and the size caps that keep the wire inside the
daemon's schema. A missing Talon channel fails loudly; the caller then
falls back to the ffmpeg crop.
"""

import base64
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import talon_channel as channel
except Exception:  # never: stdlib-only sibling, same directory
    channel = None

from mac_zoom_crop import CROP_H, CROP_W

# File-byte cap: base64 of this many bytes is at most 133804 chars,
# under the daemon schema ceiling (140000) with margin to spare. The
# cap is on the WIRE size that matters, not an arbitrary round number.
JPEG_CAP = 100352
# PNG read cap: a 480x360 UI crop never approaches this; over is refused.
PNG_CAP = 524288
CAPTURE_IMAGE = "capture_image.png"
JPEG_QUALITY = 70


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
