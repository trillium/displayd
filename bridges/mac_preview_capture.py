"""Capturing one display preview: bounded ffmpeg to a JPEG in memory.

Single concept: the per-display capture mechanics behind the preview feed --
which ffmpeg binary holds the Screen Recording grant, the pipe-only argv
(capture + scale + JPEG to stdout, never a temp file), the bounded
subprocess with its output floor and cap, and the base64 encode that goes on
the wire. Capture runs OUT OF PROCESS so no ObjC pixel buffer can leak, and
a display that refuses capture is skipped rather than killing the set.
"""

import base64
import io
import logging
import os
import subprocess

LOG = logging.getLogger("mac-preview")

PREVIEW_MAX_W = 480  # downscale bound: map rects are ~800 px panel-side
PREVIEW_QUALITY = 60  # JPEG quality: readable text shapes, small wire

FRAME_JPEG_CAP = 40000

# Capture binary search order. Screen capture needs the binary that
# holds the Screen Recording grant: the Homebrew build, verified live
# 2026-09-29 returning real pixels on this Mac (see Permission below).
# The nix-darwin system ffmpeg is a fallback only, never the default,
# so the grant story stays single; DISPLAYD_FFMPEG always wins.
FFMPEG_CANDIDATES = (
    "/opt/homebrew/bin/ffmpeg",
    "/run/current-system/sw/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
)


def resolve_ffmpeg(env=None, exists=os.path.exists, candidates=FFMPEG_CANDIDATES):
    """First existing capture-capable ffmpeg; DISPLAYD_FFMPEG overrides.

    Never raises: a non-existent path or a broken existence probe falls
    through to the next candidate, and no candidate means a bare
    ``ffmpeg`` resolved from PATH (the caller reports the failure by
    skipping the display, never by dying)."""
    env = os.environ if env is None else env
    try:
        override = env.get("DISPLAYD_FFMPEG")
    except Exception:
        override = None
    if override:
        return override
    for path in candidates:
        try:
            if exists(path):
                return path
        except Exception:
            continue
    return "ffmpeg"


# Capture binary: resolved once, override with DISPLAYD_FFMPEG.
FFMPEG_BIN = resolve_ffmpeg()
# Per-display capture bound: a hung ffmpeg must never stall the tick.
FFMPEG_TIMEOUT = 15.0
# Floor for piped output: anything smaller is a partial/empty write.
FFMPEG_MIN_BYTES = 128


def ffmpeg_quality(quality=PREVIEW_QUALITY):
    """PIL-style quality (0-100, higher is better) -> ffmpeg -q:v (2-31,
    lower is better). q60 maps to ~6, the live-verified operating point
    (~9 KB at 480 px, well under FRAME_JPEG_CAP)."""
    try:
        q = int(quality)
    except (TypeError, ValueError):
        q = PREVIEW_QUALITY
    return max(2, min(20, (100 - max(0, min(100, q))) // 10 + 2))


def ffmpeg_cmd(display_index, max_w=PREVIEW_MAX_W, quality=PREVIEW_QUALITY):
    """argv capturing + scaling + JPEG-encoding one display to stdout.
    Pipe-only: ``-f mjpeg -`` writes the single frame to stdout, never
a temp file. The display is addressed by AVFoundation screen name
(``Capture screen N``), which tracks the enumeration order Python
sees via CGGetActiveDisplayList."""
    return [FFMPEG_BIN, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-f", "avfoundation", "-framerate", "2",
            "-i", "Capture screen %d:none" % int(display_index),
            "-frames:v", "1",
            "-vf", "scale=%d:-1" % int(max_w),
            "-q:v", str(ffmpeg_quality(quality)),
            "-f", "mjpeg", "-"]


def ffmpeg_frame(display_index, max_w=PREVIEW_MAX_W, quality=PREVIEW_QUALITY,
                 timeout=FFMPEG_TIMEOUT, cap=FRAME_JPEG_CAP):
    """One display -> (jpeg bytes, w, h) via a bounded ffmpeg subprocess.

    None on any failure (grant revoked, display asleep, ffmpeg hung or
    missing, over cap, undecodable): the caller SKIPS the display, so
    the panel shows a box instead of killing the whole set. Never
    raises; never touches ObjC pixel buffers in-process (that path
    leaked ~25 MB/s, task-gjw6e). PIL only reads the JPEG header for
dimensions -- pure refcounted objects, no Create-rule ownership."""
    try:
        proc = subprocess.run(ffmpeg_cmd(display_index, max_w, quality),
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
    except Exception as err:
        LOG.debug("display %d ffmpeg failed: %s", display_index, err)
        return None
    data = bytes(proc.stdout or b"")
    if proc.returncode != 0 or len(data) < FFMPEG_MIN_BYTES:
        LOG.debug("display %d ffmpeg rc=%s bytes=%d: %s", display_index,
                  proc.returncode, len(data),
                  bytes(proc.stderr or b"")[:200])
        return None
    if len(data) > cap:
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
    return (data, int(w), int(h))


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
