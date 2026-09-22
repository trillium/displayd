"""Live stream monitor for the jumbotron: recent frame, fullscreen.

Two frame sources, push wins over poll:

- PUSH: POST /feed/stream/frame {"data": "<base64 jpeg/png>"} (or {"url": ...}).
  Buffer holds the latest frame only, so a slow panel drops stale frames
  instead of lagging further behind the stream.
- POLL: params {"url": "http://<snapshot-server>/snapshot.jpg", "fps": 2}.
  The daemon pulls the snapshot itself; nothing is pushed, so the feed
  cache and the idle clock stay untouched.

Capped-fps image refresh, deliberately: full-rate video exceeds what this
renderer pipeline is built for (one JPEG decode + fullscreen resize +
framebuffer write per present). fps clamps to 0.5..5 (default 2); the
measured present rate is drawn on screen next to LIVE.

Start/stop is one action: POST /show {"renderer": "stream", "params": {...}}
to start, POST /show (another view) or /clear to stop.

No credentials anywhere by design: there are no auth/header params, and
url accepts http(s) only. Serve snapshots from a tailnet-bound,
unauthenticated endpoint -- the same trust model as displayd itself.
"""

import base64
import hashlib
import io
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFont

NAME = "stream"
DESCRIPTION = ("Live stream monitor: latest pushed frame or polled snapshot, "
               "fullscreen at capped fps")
STATIC = False
# Playlist progress-bar colour for this view (see playlist.accent_for).
ACCENT = "#FF4D4D"
PARAMS = {
    "url": {"type": "string",
            "help": "snapshot JPEG URL to poll, e.g. http://mac:8080/snapshot.jpg"},
    "fps": {"type": "number",
            "help": "refresh cap, frames/sec: 0.5..5, default 2"},
    "fit": {"type": "string", "help": "cover (default), contain, or stretch"},
    "background": {"type": "string", "help": "letterbox colour, default black"},
    "label": {"type": "string", "help": "status tag text, default LIVE"},
}
INPUTS = {
    "frame": {
        "type": "object",
        "help": "one video frame: {data: base64 jpeg/png} or {url: http(s) snapshot}",
        "properties": {
            "data": {"type": "string"},
            "url": {"type": "string"},
            "format": {"type": "string"},
        },
        # Latest frame only: a panel that falls behind drops frames
        # instead of growing a backlog it can never catch up to.
        "buffer": 1,
    },
}

MIN_FPS = 0.5
MAX_FPS = 5.0
DEFAULT_FPS = 2.0
FETCH_TIMEOUT = 5.0
MAX_FRAME_BYTES = 8 * 1024 * 1024


def _clamp_fps(raw):
    try:
        fps = float(raw if raw is not None else DEFAULT_FPS)
    except (TypeError, ValueError):
        return DEFAULT_FPS
    return max(MIN_FPS, min(MAX_FPS, fps))


def _fit_cover(img, width, height):
    scale = max(width / img.width, height / img.height)
    resized = img.resize((max(1, int(img.width * scale)),
                          max(1, int(img.height * scale))))
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _fit_contain(img, width, height, bg):
    scale = min(width / img.width, height / img.height)
    fitted = img.resize((max(1, int(img.width * scale)),
                         max(1, int(img.height * scale))))
    canvas = Image.new("RGB", (width, height), bg)
    canvas.paste(fitted, ((width - fitted.width) // 2,
                          (height - fitted.height) // 2))
    return canvas


def _fetch(url, timeout):
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("snapshot url must be http(s)")
    req = urllib.request.Request(url, headers={"User-Agent": "displayd-stream/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(MAX_FRAME_BYTES + 1)


def _decode(raw):
    if len(raw) > MAX_FRAME_BYTES:
        raise ValueError("frame too large (%d bytes)" % len(raw))
    return Image.open(io.BytesIO(raw)).convert("RGB")


def _frame_from_payload(payload, timeout):
    """Raw JPEG/PNG bytes from one feed payload. Raises on anything unusable."""
    if not isinstance(payload, dict):
        raise ValueError("frame payload must be an object")
    if payload.get("data"):
        try:
            return base64.b64decode(payload["data"], validate=True)
        except Exception as err:
            raise ValueError("bad base64 frame data: %s" % err)
    if payload.get("url"):
        return _fetch(payload["url"], timeout)
    raise ValueError("frame needs 'data' or 'url'")


def _draw_status(base, label, fps_measured, stale):
    draw = ImageDraw.Draw(base)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold", 34)
    except Exception:
        font = None
    dot = (255, 70, 70) if not stale else (120, 120, 130)
    text = "%s  %.1f fps" % (label, fps_measured) if fps_measured else label
    x, y, pad = 28, 22, 14
    if font is not None:
        tw = draw.textlength(text, font=font)
        draw.rounded_rectangle([x - pad, y - pad, x + 44 + tw + pad, y + 44 + pad],
                               radius=10, fill=(0, 0, 0))
        draw.ellipse([x, y + 8, x + 28, y + 36], fill=dot)
        draw.text((x + 44, y), text, font=font, fill=(255, 255, 255))
    else:
        draw.text((x, y), "o " + text, fill=(255, 255, 255))
    return base


def _draw_idle(screen, bg, hint):
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(screen.font_path("DejaVuSans") or "", 44)
    except Exception:
        font = None
    lines = ["waiting for stream", hint]
    y = screen.H // 2 - 60
    for line in lines:
        draw.text((screen.W // 2, y), line, font=font, fill=(120, 120, 130),
                  anchor="mm")
        y += 60
    return img


def run(screen, params, stop):
    params = params or {}
    snap_url = params.get("url")
    fps = _clamp_fps(params.get("fps"))
    interval = 1.0 / fps
    fit = str(params.get("fit") or "cover").lower()
    bg = screen.color(params.get("background"), (0, 0, 0))
    label = str(params.get("label") or "LIVE")
    timeout = min(FETCH_TIMEOUT, interval)

    last_push = None      # object identity of the last consumed feed payload
    last_bytes = None     # sha1 of the last presented frame source bytes
    last_frame = None     # last good composited canvas (kept across errors)
    ema_interval = None   # measured present rate, exponential moving average
    last_present_at = None
    errors = 0

    if snap_url is not None and not (
            isinstance(snap_url, str)
            and snap_url.startswith(("http://", "https://"))):
        screen.present(_draw_idle(screen, bg, "bad snapshot url: must be http(s)"))
        return

    while not stop.is_set():
        tick = time.time()
        raw = None

        # Push wins: consume only a frame newer than the last one shown.
        try:
            pushed = screen.get_input("stream", "frame")
        except Exception:
            pushed = []
        if pushed and pushed[-1] is not last_push:
            last_push = pushed[-1]
            try:
                raw = _frame_from_payload(last_push, timeout)
            except Exception:
                errors += 1
                raw = None

        # Poll fallback: pull the snapshot URL on the fps cadence.
        if raw is None and snap_url and last_push is None:
            try:
                raw = _fetch(snap_url, timeout)
            except Exception:
                errors += 1
                raw = None

        if raw is not None:
            digest = hashlib.sha1(raw).digest()
            if digest != last_bytes:
                try:
                    img = _decode(raw)
                    if fit == "stretch":
                        canvas = img.resize((screen.W, screen.H))
                    elif fit == "contain":
                        canvas = _fit_contain(img, screen.W, screen.H, bg)
                    else:
                        canvas = _fit_cover(img, screen.W, screen.H)
                    now = time.time()
                    if last_present_at is not None:
                        gap = now - last_present_at
                        ema_interval = (gap if ema_interval is None
                                        else 0.3 * gap + 0.7 * ema_interval)
                    last_present_at = now
                    shown_fps = (1.0 / ema_interval) if ema_interval else 0.0
                    last_frame = _draw_status(canvas, label, shown_fps, False)
                    last_bytes = digest
                    errors = 0
                    screen.present(last_frame)
                except Exception:
                    errors += 1
        elif last_frame is None:
            hint = ("POST a frame to /feed/stream/frame"
                    if not snap_url else "pulling %s" % snap_url)
            screen.present(_draw_idle(screen, bg, hint))
            # Idle screen drawn once; avoid repainting it every tick.
            if snap_url is None:
                stop.wait(interval)
                continue

        # Pace the loop at the fps cap (present cost counts against it).
        elapsed = time.time() - tick
        stop.wait(max(0.01, interval - elapsed))
