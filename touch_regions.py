"""Display-space touch geometry: raw device units to display pixels, and
first-hit region testing.

Single concept: where a touch lands on the panel. ``normalize`` maps raw
device units (with calibration, swap, invert and rotation) into clamped
display pixels; ``hit_test`` returns the id of the first configured region
containing a point. No I/O and no action dispatch.
"""


# ---------------------------------------------------------------------------
# Coordinate normalization (raw device units -> display pixels)
# ---------------------------------------------------------------------------

def normalize(raw_x, raw_y, width, height, calibration=None):
    """Map raw touch units into display pixels.

    calibration keys (all optional): x_min, x_max, y_min, y_max (raw range
    reported by the device -- from `evtest` or sysfs `input/abs*` bounds),
    swap_xy, invert_x, invert_y, rotation (one of 0/90/180/270, clockwise
    degrees applied after swap/invert). Returns (x_px, y_px) clamped to
    [0, width-1] x [0, height-1]; None input on either axis yields None
    on that axis (contact without position yet).
    """
    cal = calibration or {}
    x_min = cal.get("x_min", 0)
    x_max = cal.get("x_max")
    y_min = cal.get("y_min", 0)
    y_max = cal.get("y_max")
    if x_max is None or y_max is None:
        raise ValueError("calibration needs x_max/y_max (device raw range)")
    if x_max <= x_min or y_max <= y_min:
        raise ValueError("calibration range must be non-empty")

    def scale(raw, lo, hi, size):
        if raw is None:
            return None
        frac = (raw - lo) / float(hi - lo)
        frac = max(0.0, min(1.0, frac))
        return int(round(frac * (size - 1)))

    x = scale(raw_x, x_min, x_max, width)
    y = scale(raw_y, y_min, y_max, height)
    if cal.get("swap_xy"):
        x, y = y, x
        width, height = height, width
    if x is not None and cal.get("invert_x"):
        x = (width - 1) - x
    if y is not None and cal.get("invert_y"):
        y = (height - 1) - y
    rotation = cal.get("rotation", 0)
    if rotation not in (0, 90, 180, 270):
        raise ValueError("rotation must be one of 0/90/180/270")
    if rotation and x is not None and y is not None:
        if rotation == 90:
            x, y = (width - 1) - y, x
            width, height = height, width
        elif rotation == 180:
            x, y = (width - 1) - x, (height - 1) - y
        elif rotation == 270:
            x, y = y, (height - 1) - x
            width, height = height, width
    return x, y


# ---------------------------------------------------------------------------
# Hit regions
# ---------------------------------------------------------------------------

def hit_test(x, y, regions):
    """Return the id of the first region containing (x, y), else None.

    A region is {"id": str, "rect": [x, y, w, h]} in display pixels.
    Regions are evaluated in config order, so earlier entries win overlaps.
    """
    if x is None or y is None:
        return None
    for region in regions:
        rid = region.get("id")
        rect = region.get("rect")
        if not rid or not rect or len(rect) != 4:
            continue
        rx, ry, rw, rh = rect
        if rx <= x < rx + rw and ry <= y < ry + rh:
            return rid
    return None
