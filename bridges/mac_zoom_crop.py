"""Where the zoom review crop lands: the tap point -> panel crop rect.

Single concept: given the Mac's live display geometry and the Quartz point
the user tapped, decide which display that point is on and the clamped crop
box around it. Both review-capture paths share it, so the Talon capture and
the ffmpeg fallback always frame the same area. Pure; never raises.
"""

CROP_W, CROP_H = 480, 360


def _num(value):
    return value if isinstance(value, (int, float)) \
        and not isinstance(value, bool) else None


def crop_for(x, y, displays, w=CROP_W, h=CROP_H):
    """Crop rect around (x, y), clamped inside its display.

    Returns {"x","y","w","h"} ints, or None when the point is on no
    known display (capture nothing rather than the wrong screen).
    Pure; never raises."""
    try:
        qx, qy = float(x), float(y)
        w, h = int(w), int(h)
        if w <= 0 or h <= 0:
            return None
    except (TypeError, ValueError):
        return None
    box = None
    try:
        for d in displays or []:
            b = d.get("bounds") if isinstance(d, dict) else None
            if not isinstance(b, dict):
                continue
            bx, by = _num(b.get("x")), _num(b.get("y"))
            bw, bh = _num(b.get("w")), _num(b.get("h"))
            if None in (bx, by, bw, bh) or bw <= 0 or bh <= 0:
                continue
            if bx <= qx < bx + bw and by <= qy < by + bh:
                box = (bx, by, bw, bh)
                break
    except Exception:
        return None
    if box is None:
        return None
    bx, by, bw, bh = box
    cx = min(max(int(qx - w / 2), int(bx)), int(bx + bw - w))
    cy = min(max(int(qy - h / 2), int(by)), int(by + bh - h))
    if cx < bx or cy < by:  # display smaller than the crop: whole screen
        return {"x": int(bx), "y": int(by),
                "w": int(min(w, bw)), "h": int(min(h, bh))}
    return {"x": cx, "y": cy, "w": w, "h": h}
