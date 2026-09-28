"""Proportional column widths for the beads parade (V1 direction).

NOT a renderer: no run(), so the daemon's loader skips this file, exactly
like beads_common. The overview renderer (beads.py) imports it.

V1 keeps the same four buckets in the same order with the same attention
list -- continuity is the point. The one change from equal columns: each
column's width follows its share of beads on a square-root scale, with a
minimum-width floor so Rolling stays legible.

Deliberate, reasoned compromise (do not "fix" without re-reading the
variants report in firstmate's data dir): the sqrt scale flatters small
buckets, and the floor breaks strict proportionality for Rolling and
Stalled. On the live 8,326-bead export Lined Up outnumbers Rolling 125:1
(3,628 vs 29); a linear share would erase Rolling entirely, while equal
columns -- today's panel -- quietly lie about the shape of the queue.
Sqrt plus floor is the middle that stays honest about the queue dwarfing
everything without blanking the small buckets.
"""

import math

# Minimum-width floors in pixels at panel scale, per bucket key. Rolling
# (29 beads live) and Stalled (145) would vanish on a strict share scale
# against Lined Up (3,628); Lined Up and Past the Stand need no floor.
FLOORS = {"rolling": 170.0, "linedup": 0.0, "stalled": 190.0, "past": 0.0}


def column_widths(counts, avail, floors=None):
    """Split avail pixels across buckets proportionally to sqrt(count).

    Args:
        counts: beads per bucket, in bucket order.
        avail: total pixels to split.
        floors: minimum pixels per bucket, same length as counts;
            defaults to FLOORS in bucket order.

    Guarantee: the returned widths always tile avail exactly (up to float
    rounding) -- a column is never dropped and the strip never overflows.

    Degenerate cases: all buckets empty -> equal columns (nothing to say);
    avail too small for the floors themselves -> floors scaled down to
    fit; a bucket sitting exactly on its floor keeps exactly the floor.
    """
    counts = [max(0, int(c)) for c in counts]
    n = len(counts)
    if n == 0 or avail <= 0:
        return [0.0] * n
    if floors is None:
        floors = [FLOORS[key] for key in
                  ("rolling", "linedup", "stalled", "past")[:n]]
    floors = [max(0.0, float(f)) for f in floors]
    weights = [math.sqrt(c) for c in counts]
    total = sum(weights)
    if total <= 0:
        return [avail / n] * n
    raw = [avail * w / total for w in weights]
    # Floors take from the columns above them, in proportion to their
    # raw share -- the mockup algorithm in make_mockups.py, ported, plus
    # pin-and-redistribute so a column can never be driven below its
    # floor (the bare mockup formula max()s instead, which breaks the
    # tiling guarantee when the take exceeds the flex).
    widths = [max(f, r) for f, r in zip(floors, raw)]
    take = sum(widths) - avail
    above = [i for i in range(n) if widths[i] > floors[i]]
    while take > 1e-9 and above:
        tot = sum(widths[i] for i in above)
        if tot <= 0:
            break
        applied = 0.0
        for i in above:
            cut = min(take * (widths[i] / tot), widths[i] - floors[i])
            widths[i] -= cut
            applied += cut
        take -= applied
        if applied < 1e-9:
            break
        above = [i for i in above if widths[i] > floors[i] + 1e-9]
    if take > 1e-9:
        # The floors themselves exceed avail: scale everything to fit.
        scale = sum(widths)
        if scale <= 0:
            return [avail / n] * n
        widths = [w * avail / scale for w in widths]
    return widths
