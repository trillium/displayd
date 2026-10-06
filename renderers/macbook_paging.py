"""App-strip paging arithmetic for the merged macbook feature.

Single concept: the pure window arithmetic behind the header's tab strip --
six visible chip slots, a window start clamped to the valid page range, one
stepper press moving the window by all-but-one chip (so consecutive pages
overlap by one chip and the user stays oriented), and the slot indices a
window puts on screen. No geometry and no pixels: macbook_layout.py owns
the rects these slots map onto.
"""

VISIBLE = 6
# One stepper press pages the strip by all-but-one of the visible
# chips: the new window overlaps the old by exactly one chip, which
# keeps the user oriented while every press visibly changes the bar.
PAGE_STRIDE = VISIBLE - 1


def page_start(start, count, visible=VISIBLE):
    """First visible slot for a window start: clamped to the page range.

    Valid starts are [0, max(0, count - visible)]; anything outside
    (including a stale start after the app list shrank) pins to the
    nearest valid page. Never raises."""
    try:
        count = int(count)
    except (TypeError, ValueError):
        return 0
    try:
        visible = int(visible)
    except (TypeError, ValueError):
        visible = VISIBLE
    if count <= 0 or visible <= 0:
        return 0
    if count <= visible:
        return 0
    try:
        start = int(start)
    except (TypeError, ValueError):
        start = 0
    return max(0, min(count - visible, start))


def page(start, direction, count, visible=VISIBLE):
    """Page the strip one window in `direction` (+1/-1), clamped.

    Returns (new_start, moved): each press moves PAGE_STRIDE chips
    (VISIBLE - 1, so the windows overlap by one chip) and stops dead
    at either end -- no wraparound, so a press never jumps the user
    from the last page back to the first. `moved` is False when the
    strip is already at that end (or fits on one page); the caller
    should refuse the press and the renderer dims that stepper.
    Never raises."""
    try:
        count = int(count)
    except (TypeError, ValueError):
        return (0, False)
    try:
        visible = int(visible)
    except (TypeError, ValueError):
        visible = VISIBLE
    if count <= visible:
        return (0, False)
    cur = page_start(start, count, visible)
    try:
        direction = int(direction)
    except (TypeError, ValueError):
        return (cur, False)
    if direction == 0:
        return (cur, False)
    stride = max(1, visible - 1)
    if direction > 0:
        new = min(cur + stride, count - visible)
    else:
        new = max(cur - stride, 0)
    return (new, new != cur)


def page_bounds(start, count, visible=VISIBLE):
    """Stepper state for a window start: (at_first, at_last).

    True means that end's stepper is dead (a press would be refused)
    and must be drawn dim. A single-page strip reports both. Never
    raises."""
    try:
        count = int(count)
    except (TypeError, ValueError):
        return (True, True)
    if count <= visible:
        return (True, True)
    cur = page_start(start, count, visible)
    return (cur <= 0, cur + visible >= count)


def page_slots(start, count, visible=VISIBLE):
    """Slot indices on screen for a window start: (start, [slot, ...])."""
    cur = page_start(start, count, visible)
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0
    try:
        visible = int(visible)
    except (TypeError, ValueError):
        visible = VISIBLE
    return (cur, list(range(cur, min(count, cur + visible))))
