"""Local images for the html painter: the path trust boundary
(:func:`allow_root`, :func:`resolve_local`), the decoded-image cache, and the
repeat compositing.
"""

import os
import threading

from PIL import Image

from _html_abi import (REPEAT_NO_REPEAT, REPEAT_REPEAT, REPEAT_REPEAT_X,
                       REPEAT_REPEAT_Y)
from _html_geom import _visible

MAX_IMAGES = 8
MAX_IMAGE_PIXELS = 4096 * 4096

def allow_root(root):
    """Normalise a trusted local directory. Symlinks resolved once here."""
    return os.path.realpath(os.path.expanduser(root))


def resolve_local(root, ref):
    """Map a template-relative reference to a real path inside root.

    Returns None for anything that is not a plain relative path landing
    inside root: absolute paths, URLs, '..', and empty refs are all
    refused. This is the only place a document can name a file.
    """
    if not isinstance(ref, str) or not ref or "\x00" in ref:
        return None
    ref = ref.split("#", 1)[0].split("?", 1)[0].strip()
    if not ref or "://" in ref or ref.startswith("//") or ref.startswith("data:"):
        return None
    if ref.startswith(("/", "~", "\\")) or ":" in ref:
        return None
    path = os.path.realpath(os.path.join(root, ref))
    if path != root and not path.startswith(root + os.sep):
        return None
    if not os.path.isfile(path):
        return None
    return path


class _ImageCache:
    """Decoded local images, bounded, keyed by path and target size."""

    def __init__(self, limit=MAX_IMAGES):
        self.limit = limit
        self._lock = threading.Lock()
        self._decoded = {}
        self._scaled = {}

    def _decode(self, path):
        with self._lock:
            hit = self._decoded.get(path)
            if hit is not None:
                return hit
        try:
            with Image.open(path) as probe:
                if probe.width * probe.height > MAX_IMAGE_PIXELS:
                    return None
                img = probe.convert("RGBA")
        except Exception:
            return None
        with self._lock:
            self._decoded[path] = img
            while len(self._decoded) > self.limit:
                self._decoded.pop(next(iter(self._decoded)))
        return img

    def size(self, url, base_url, root):
        path = self._path(url, base_url, root)
        if not path:
            return 0.0, 0.0
        img = self._decode(path)
        return (0.0, 0.0) if img is None else (float(img.width), float(img.height))

    def _sized(self, path, box_w, box_h):
        key = (path, box_w, box_h)
        with self._lock:
            hit = self._scaled.get(key)
            if hit is not None:
                return hit
        img = self._decode(path)
        if img is None:
            return None
        if (img.width, img.height) != (box_w, box_h):
            try:
                img = img.resize((box_w, box_h), Image.BILINEAR)
            except Exception:
                return None
        with self._lock:
            self._scaled[key] = img
            while len(self._scaled) > self.limit:
                self._scaled.pop(next(iter(self._scaled)))
        return img

    def draw(self, canvas, url, base_url, box, area, clip, repeat, root):
        path = self._path(url, base_url, root)
        if not path:
            return
        x0, y0, x1, y1 = box
        tw, th = int(x1 - x0), int(y1 - y0)
        if tw <= 0 or th <= 0:
            return
        # The tile is `box`; the region to cover is `area` (the element's
        # border box) cut to the clip. Tiling inside `box` instead would draw
        # exactly one tile no matter what repeat says.
        ax0, ay0, ax1, ay1 = _visible(area, clip) or (0, 0, 0, 0)
        if not (ax1 > ax0 and ay1 > ay0):
            return
        img = self._sized(path, tw, th)
        if img is None:
            return
        if repeat == REPEAT_NO_REPEAT:
            _paste_clipped(canvas, img, x0, y0, (ax0, ay0, ax1, ay1))
            return
        # Repeat: compose the whole covered region into one image and paste it
        # once. Walking tile by tile needs a cap to stay safe, and a cap
        # truncates in raster order -- the panel came out half-painted with a
        # black corner, which is worse than slow. Doubling the grid instead
        # costs O(log tiles) pastes and is exact at any tile size.
        ox = max(0, int(x0) - int(ax0)) if repeat in _REPEATS_X else 0
        oy = max(0, int(y0) - int(ay0)) if repeat in _REPEATS_Y else 0
        need_w = int(ax1 - ax0) + ox if repeat in _REPEATS_X else tw
        need_h = int(ay1 - ay0) + oy if repeat in _REPEATS_Y else th
        grid = _compose_repeat(img, need_w, need_h, repeat)
        _paste_clipped(canvas, grid, int(ax0) - ox, int(ay0) - oy,
                       (ax0, ay0, ax1, ay1))

    def _path(self, url, base_url, root):
        if url is None or not root:
            return None
        ref = url.decode("utf-8", "replace") if isinstance(url, bytes) else str(url)
        if not ref or base_url:
            # A base url means the document tried to resolve a document-level
            # base; refuse rather than guess. Templates are single files.
            return None
        return resolve_local(root, ref)


_REPEATS_X = (REPEAT_REPEAT, REPEAT_REPEAT_X)
_REPEATS_Y = (REPEAT_REPEAT, REPEAT_REPEAT_Y)


def _compose_repeat(tile, width, height, repeat):
    """An exactly width x height image of `tile` repeated, by doubling.

    Each round pastes the current grid next to itself, so reaching N tiles
    costs log2(N) pastes and the result is a true pixel repeat -- no
    stretching, no seam. Only whole tiles are added, then the last round is
    cropped, so the size is never over target by more than one tile.
    """
    width = max(1, int(width))
    height = max(1, int(height))
    for axis, target in ((0, width if repeat in _REPEATS_X else tile.width),
                         (1, height if repeat in _REPEATS_Y else tile.height)):
        img = tile
        while (img.width if axis == 0 else img.height) < target:
            cur = img.width if axis == 0 else img.height
            grow = min(cur * 2, target)
            size = (grow, img.height) if axis == 0 else (img.width, grow)
            bigger = Image.new(img.mode, size)
            bigger.paste(img, (0, 0))
            extra = grow - cur
            if axis == 0:
                bigger.paste(img.crop((0, 0, extra, img.height)), (cur, 0))
            else:
                bigger.paste(img.crop((0, 0, img.width, extra)), (0, cur))
            img = bigger
        if axis == 0:
            tile = img
        else:
            tile = _combine(tile, img)
    return tile


def _combine(width_image, height_image):
    """A grid as wide as `width_image` and as tall as `height_image`.

    Built by cropping both to the shared corner rather than re-tiling: both
    are already exact repeats of the same tile, so their overlap is a repeat
    too.
    """
    if width_image is height_image:
        return width_image
    out = Image.new(width_image.mode, (width_image.width, height_image.height))
    out.paste(width_image, (0, 0))
    out.paste(height_image.crop((0, 0, width_image.width, height_image.height)),
              (0, 0))
    return out


def _paste_clipped(canvas, img, x, y, area):
    """Paste with a clip without allocating a full-size scratch."""
    x, y = int(x), int(y)
    ax0, ay0, ax1, ay1 = (int(v) for v in area)
    ix0, iy0 = max(0, ax0 - x), max(0, ay0 - y)
    ix1 = min(img.width, ax1 - x)
    iy1 = min(img.height, ay1 - y)
    if ix1 <= ix0 or iy1 <= iy0:
        return
    piece = img if (ix0, iy0, ix1, iy1) == (0, 0, img.width, img.height) \
        else img.crop((ix0, iy0, ix1, iy1))
    canvas.paste(piece, (x + ix0, y + iy0), piece)
