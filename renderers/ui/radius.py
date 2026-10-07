"""Anti-aliased rounded-rectangle coverage masks, as a component.

The shape a rounded box covers, in the one form PIL can paste through: an
"L" mode coverage mask. It is a component like `ui.tile` or `ui.panel`
because a rounded box is the same thing whatever asked for it -- the html
painter reaches it for `border-radius`, and any other surface will reach it
the same way.

litehtml resolves `border-radius` to eight per-corner pixel radii and hands
them over in the C ABI (``lhtml_radii``). This module turns one box plus
those eight numbers into that mask.

WHY A SUPERSAMPLED MASK AND NOT ``ImageDraw.rounded_rectangle``
    PIL's own primitive takes a single scalar radius and a `corners` tuple
    that only *selects* corners -- it cannot express eight independent
    values, and it is hard-edged (measured on a 160x120 box at r=40: two
    distinct colours in the corner, i.e. a visible staircase on a 1080p
    panel). So the arc is drawn once at ``SUPERSAMPLE``x and box-filtered
    back down, which is what produces the intermediate coverage levels.

WHY ONLY THE CORNERS ARE SUPERSAMPLED
    The straight runs of a rounded rectangle sit exactly on integer pixel
    boundaries, so they need no anti-aliasing at all. Only the four corner
    quadrants do, and each is a small ``(rx+1) x (ry+1)`` patch. Rendering
    the whole box at 4x instead costs a 7680x4320 scratch image for a
    full-HD panel; this way a full-HD rounded panel builds in ~0.2 ms.

WHY CACHING IS SAFE
    The mask is a pure function of exactly ``(width, height, the eight
    radii)`` and nothing else: not the colour, not the canvas, not the
    device scale (litehtml already resolved every value into border-box
    pixels for the one viewport being painted). So an entry cannot go stale
    -- a frame with a different colour reuses the same shape, and a frame
    with a different radius misses the cache and builds a new one. It is
    bounded by ``MAX_MASKS`` with first-in eviction, the same discipline as
    the font and image caches in the html renderer, and the cost per render
    is one dict lookup plus a paste.
"""

import threading

from PIL import Image, ImageDraw, ImageOps

# Supersample factor for the corner arcs. 4 is where the measured area of a
# uniform-radius box is within 0.03% of the analytic value; 8 buys a further
# 0.01% and costs twice as much, so it is not worth it.
SUPERSAMPLE = 4

# Corner order matches lhtml_radii: top-left, top-right, bottom-right,
# bottom-left. Each entry is (mirror_x, mirror_y) for the one arc shape we
# build, which is always the top-left quadrant.
_CORNERS = ((False, False), (True, False), (True, True), (False, True))

# Bounded like the html renderer's other caches: a view that animates a
# radius would otherwise grow this without limit.
MAX_MASKS = 32

_cache = {}
_cache_lock = threading.Lock()


def live():
    """Mask entries held right now, so a leak is observable from a test."""
    with _cache_lock:
        return len(_cache)


def clear():
    """Drop every cached mask. Tests and a renderer reload use this."""
    with _cache_lock:
        _cache.clear()


def _corner(rx, ry, mirror_x, mirror_y):
    """One corner's coverage over its (rx+1 x ry+1) arc quadrant.

    The quadrant holds the quarter ellipse AND the two straight runs that
    meet it, so pasting it leaves the rest of the box untouched. Drawn
    top-left always, then mirrored into place: PIL's pieslice fills a
    quadrant of the FULL ellipse, three quarters of which falls outside the
    quadrant we keep, so the other corners are flips of this one.
    """
    w, h = rx + 1, ry + 1
    big = Image.new("L", (w * SUPERSAMPLE, h * SUPERSAMPLE), 0)
    draw = ImageDraw.Draw(big)
    # bbox [0, 0, 2rx, 2ry] puts the ellipse centre at exactly (rx, ry) and
    # both axes at exactly rx, ry, which is where CSS puts them.
    draw.pieslice([0, 0, 2 * rx * SUPERSAMPLE, 2 * ry * SUPERSAMPLE],
                  180, 270, fill=255)
    draw.rectangle([rx * SUPERSAMPLE, 0,
                    w * SUPERSAMPLE - 1, h * SUPERSAMPLE - 1], fill=255)
    draw.rectangle([0, ry * SUPERSAMPLE,
                    w * SUPERSAMPLE - 1, h * SUPERSAMPLE - 1], fill=255)
    patch = big.resize((w, h), Image.BOX)  # BOX = exact area average
    if mirror_x:
        patch = ImageOps.mirror(patch)     # NOTE: mirror is horizontal,
    if mirror_y:                           # flip is vertical
        patch = ImageOps.flip(patch)
    return patch


def rounded(w, h, radii):
    """An "L" mask: 255 inside a rounded rectangle, 0 outside, soft between.

    `radii` is the eight (x, y) pairs in lhtml_radii order. Radii are
    clamped to half the box per axis, which keeps the four arc quadrants
    disjoint: at exactly half they would overlap, and which corner won the
    shared pixel would depend on paste order. Sub-pixel cost, and a pill
    still reads as a pill.
    """
    box = []
    for i in range(0, 8, 2):
        rx = min(int(radii[i]), (w - 1) // 2)
        ry = min(int(radii[i + 1]), (h - 1) // 2)
        box.append((max(0, rx), max(0, ry)))
    mask = Image.new("L", (w, h), 255)
    for i, (rx, ry) in enumerate(box):
        if rx <= 0 or ry <= 0:
            continue
        if i == 0:
            at = (0, 0)
        elif i == 1:
            at = (w - 1 - rx, 0)
        elif i == 2:
            at = (w - 1 - rx, h - 1 - ry)
        else:
            at = (0, h - 1 - ry)
        mask.paste(_corner(rx, ry, *_CORNERS[i]), at)
    return mask


def shape(w, h, radii):
    """Cached `rounded`. `radii` is the 8-tuple key; see the module docstring
    for why holding it across frames cannot go stale."""
    key = (int(w), int(h)) + tuple(int(v) for v in radii)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            return hit
    mask = rounded(key[0], key[1], key[2:])
    with _cache_lock:
        _cache[key] = mask
        while len(_cache) > MAX_MASKS:
            _cache.pop(next(iter(_cache)))
    return mask


def blank():
    """The all-zero case, as a fully opaque rectangle.

    This is what makes an absent or malformed radius render EXACTLY as it did
    before radii existed: every pixel is 255, so pasting through this mask
    is indistinguishable from the old ImageDraw.rectangle.
    """
    return Image.new("L", (1, 1), 255)