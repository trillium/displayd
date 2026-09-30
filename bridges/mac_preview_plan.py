"""Pure planning rules for the live preview tick (bridges/mac_preview.py).

No I/O, no clock, no PIL: mac_preview.py keeps display enumeration,
capture, POST, and the wire format; this module owns the three decisions
that set frame age and wire volume --

  * next_display_index: which display a tick captures (the stagger);
  * merge_frames / frame_list: how a fresh frame folds into the
    last-known set that gets POSTed;
  * frame_signature / may_skip_post: when a byte-identical set may skip
    the wire POST.

Keeping them pure is what makes the frame-age claim measurable: the
hermetic virtual-clock harness in tests/test_macbook_preview.py drives
the real preview loop through these rules with no hardware, and
tests/test_mac_preview_plan.py pins each rule directly.

Why stagger (one display per tick, round-robin)
-----------------------------------------------
ffmpeg screen capture is latency-bound, not CPU-bound: opening the
AVFoundation device dominates (~1.5 s wall vs ~0.4 s CPU per display on
the measured MacBook). Capturing both displays in one tick -- the
batched baseline -- ages the FIRST display by the SECOND display's
capture wall time before either is POSTed, so its delivery age (capture
-> first POST) is ~3 s. One display per tick POSTs each frame right
after its own capture (~1.5 s, i.e. half) and each capture still runs
alone with the whole encode budget. Captures per second do not change
(two captures per round either way), so per-set CPU stays at-or-below
the batched baseline.

Why a merged set (never a single frame)
---------------------------------------
The feed carries one document (buffer 1) and the map draws a box for any
display with no frame (renderers/macbook_preview.by_display), so a
staggered POST must restate the other display's last-known frame or the
map would flicker to a box on every other tick. merge_frames also drops
a display whose capture failed: that is exactly the batched baseline's
box semantics, never a lingering stale photo.

Why dedup is opportunistic (freshness first)
--------------------------------------------
The panel marks the feed STALE at PREVIEW_FRESH = 3.0 s of document age
(renderers/macbook_preview.py), so the gap between POSTs must stay
inside that window with margin for clock skew and wire time. A
byte-identical set may therefore skip the POST only when the tick after
the skip still lands inside FRESH_BUDGET, which makes the skip cadence a
function of the tick period: at the measured ~1.7 s tick it degrades to
one POST per tick (no skips), and on a faster tick it skips real wire.
The live badge is never traded for wire savings either way.
"""

import hashlib

# POST gap ceiling: the renderer's PREVIEW_FRESH (3.0 s) minus margin
# for clock skew and wire time. tests/test_mac_preview_plan.py pins
# FRESH_BUDGET < PREVIEW_FRESH so the two constants cannot drift.
FRESH_BUDGET = 2.8


def next_display_index(turn, count):
    """Display index tick `turn` should capture, or None (nothing to do).

    Round-robin is the stagger: consecutive ticks capture different
    displays, so each capture runs alone with the whole encode budget
    and its frame is POSTed as soon as it exists. Never raises."""
    try:
        total = int(count)
        if total <= 0:
            return None
        return int(turn) % total
    except (TypeError, ValueError):
        return None


def frame_list(known):
    """{display_index: frame} -> frames ordered by display_index.

    Ordering is cosmetic (the renderer keys by display_index) but it
    keeps a signature stable across dict insertion order; unparseable
    indices are skipped. Never raises."""
    try:
        pairs = []
        for key, value in dict(known or {}).items():
            try:
                pairs.append((int(key), value))
            except (TypeError, ValueError):
                continue
        return [value for _key, value in sorted(pairs)]
    except Exception:
        return []


def merge_frames(known, index, frame, count=None):
    """Fold one capture result into the last-known frame set.

    `frame` None means that display was not captured this tick (ffmpeg
    failed, output over cap, or an undecodable frame): the display is
    DROPPED so the map shows a box exactly like the batched baseline's
    missing frame. Indices at or past `count` are dropped too, so a
    disconnected display cannot linger as a stale photo. Returns a new
    dict (`known` is never mutated). Never raises; garbage index returns
    the input set unchanged."""
    out = {}
    try:
        for key, value in dict(known or {}).items():
            try:
                out[int(key)] = value
            except (TypeError, ValueError):
                continue
        try:
            fresh = int(index)
        except (TypeError, ValueError):
            return out
        if frame is None:
            out.pop(fresh, None)
        else:
            out[fresh] = frame
        if count is not None:
            limit = int(count)
            for key in [k for k in out if k < 0 or k >= limit]:
                out.pop(key, None)
    except Exception:
        return dict(known or {})
    return out


def frame_signature(frames):
    """Content hash of a frame set; ts-independent and order-free.

    Two sets are byte-identical for the wire iff their signatures match:
    the document ts is deliberately outside the signature (it changes on
    every POST) and each frame is hashed per display, so reordering the
    list still compares equal. "" means "nothing postable" -- and every
    consumer reads "" as "do not skip". Never raises."""
    try:
        parts = []
        for frame in frames or []:
            if not isinstance(frame, dict):
                raise ValueError("frame")
            digest = hashlib.sha256(
                str(frame.get("jpeg") or "").encode("utf-8")).hexdigest()
            parts.append("%d:%sx%s:%s" % (int(frame["display_index"]),
                                          frame.get("w"), frame.get("h"),
                                          digest))
        if not parts:
            return ""
        joined = "|".join(sorted(parts)).encode("utf-8")
        return hashlib.sha256(joined).hexdigest()
    except Exception:
        return ""


def may_skip_post(signature, last_signature, now, last_post_at, next_period,
                  budget=FRESH_BUDGET):
    """True when a byte-identical set may skip this wire POST.

    False (POST) whenever there is nothing to compare, the set changed,
    or skipping could age the feed past `budget` before the tick after
    this one POSTs. Garbage in also POSTs: a redundant POST is always
    cheaper than a panel that goes STALE. Never raises."""
    try:
        if not signature or signature != last_signature:
            return False
        return (float(now) + max(0.0, float(next_period))
                - float(last_post_at)) < float(budget)
    except (TypeError, ValueError):
        return False
