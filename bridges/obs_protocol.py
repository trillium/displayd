"""obs-websocket v5 pure protocol (no I/O, no sockets).

Single concept: the vendor-protocol pure functions the screenshot bridge
needs -- fps caps (mirroring ``renderers/stream.py``), the Identify
authentication string, the fps clamp, and the screenshot payload strip.
All are pure functions or constants, direct test hooks. The bridge
(``obs_poll.py``) re-exports these names so existing importers keep working.

Protocol notes (obs-websocket 5.x -- do not re-derive):
  * server opens with Hello ``{"op": 0, "d": {"rpcVersion": 1, ...}}``;
    when a password is set, ``d.authentication`` carries salt+challenge;
  * answer with Identify ``{"op": 1, ...}`` (eventSubscriptions 0: this
    bridge wants request/response only, no events); server replies
    Identified ``{"op": 2, ...}``;
  * screenshot is request ``GetSourceScreenshot`` (op 6), answered by op 7
    with ``responseData.imageData`` as ``data:image/...;base64,....``.
"""

import base64
import hashlib
import random

# Renderer caps, mirrored from renderers/stream.py (single source of truth
# for the cap lives in the renderer; this copy only keeps the poller from
# out-pacing what the panel can present).
MIN_FPS = 0.5
MAX_FPS = 5.0
DEFAULT_FPS = 2.0

BACKOFF_FIRST = 1.0
BACKOFF_MAX = 30.0
HEALTHY_RESET_AFTER = 30.0  # a connection living this long resets the ladder
REQUEST_TIMEOUT_PAD = 2.0   # extra seconds beyond one frame interval
IMAGE_WIDTH = 960           # screenshot width cap: keeps frames panel-sized
IMAGE_QUALITY = 92          # jpeg quality: crisp frames, still small


def obs_auth(password, salt, challenge):
    """Authentication string for Identify. Pure function (test hook)."""
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode()).digest()).decode()


def clamp_fps(raw):
    """Mirror of renderers/stream.py _clamp_fps (pure function, test hook)."""
    try:
        fps = float(raw if raw is not None else DEFAULT_FPS)
    except (TypeError, ValueError):
        return DEFAULT_FPS
    return max(MIN_FPS, min(MAX_FPS, fps))


def strip_data_prefix(image_data):
    """'data:image/jpeg;base64,....' -> raw base64 (what /feed wants)."""
    if not isinstance(image_data, str) or not image_data:
        raise ValueError("empty screenshot payload")
    if "," in image_data and image_data.startswith("data:"):
        return image_data.split(",", 1)[1]
    return image_data


def next_backoff(uptime, backoff, jitter=None):
    """One reconnect-ladder step. Pure function (test hook).

    A connection living longer than HEALTHY_RESET_AFTER resets the
    ladder; otherwise the delay doubles up to BACKOFF_MAX, with
    +/-20% jitter so colliding bridges do not retry in lockstep.
    Returns (new_backoff, sleep_seconds)."""
    if uptime > HEALTHY_RESET_AFTER:
        backoff = BACKOFF_FIRST  # healthy run; reset the ladder
    else:
        backoff = min(backoff * 2, BACKOFF_MAX)
    roll = jitter() if jitter is not None else random.random()
    return backoff, min(backoff, BACKOFF_MAX) * (0.8 + 0.4 * roll)
