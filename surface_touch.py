"""Touch events from a surface: validation against what it declared.

Single concept: a surface may only report a touch interaction it said it
supports, inside a region that takes touch, at a point inside its usable
viewport. ``build_event`` is the whole check; the result is the event a
consumer later reads from ``GET /surfaces/<id>/events``.

Coordinates are surface viewport pixels. When the sender names no region
the point decides: the smallest touch-enabled region containing it (ties go
to the one declared first), so overlapping regions behave like layers.
"""

from surface_model import MULTI_POINT, TOUCH_INTERACTIONS, SurfaceError

NUMERIC_EXTRAS = ("dx", "dy", "scale", "duration_ms")


def _num(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s: expected a number" % where)
    return value


def _contains(rect, x, y):
    return (rect["x"] <= x < rect["x"] + rect["w"]
            and rect["y"] <= y < rect["y"] + rect["h"])


def region_at(surface, x, y):
    """The touch region that owns a point, or None."""
    hits = [r for r in surface["regions"]
            if r["touch"] and _contains(r["rect"], x, y)]
    if not hits:
        return None
    return min(hits, key=lambda r: r["rect"]["w"] * r["rect"]["h"])


def build_event(surface, body):
    """Validate one reported touch; returns the event (no seq/time yet)."""
    if not isinstance(body, dict):
        raise ValueError("touch event: expected an object")
    touch = surface["touch"]
    if not touch["supported"]:
        raise SurfaceError(409, "surface %r has no touch" % surface["id"])
    kind = body.get("interaction")
    if kind not in TOUCH_INTERACTIONS:
        raise ValueError("interaction: expected one of %s"
                         % ", ".join(TOUCH_INTERACTIONS))
    if kind not in touch["interactions"]:
        raise SurfaceError(409, "surface %r does not support %r (supports %s)"
                           % (surface["id"], kind, ", ".join(touch["interactions"])))
    x, y = _num(body.get("x"), "x"), _num(body.get("y"), "y")
    usable = surface["viewport"]["usable"]
    if not _contains({"x": usable["x"], "y": usable["y"],
                      "w": usable["w"], "h": usable["h"]}, x, y):
        raise ValueError("x,y: outside the usable viewport")
    points = body.get("points", 2 if kind in MULTI_POINT else 1)
    if isinstance(points, bool) or not isinstance(points, int) or points < 1:
        raise ValueError("points: expected a positive integer")
    if kind in MULTI_POINT and points < 2:
        raise ValueError("points: %s needs at least 2" % kind)
    if points > touch["max_points"]:
        raise SurfaceError(409, "surface %r reports at most %d touch points"
                           % (surface["id"], touch["max_points"]))
    named = body.get("region")
    if named is not None:
        region = next((r for r in surface["regions"] if r["name"] == named), None)
        if region is None:
            raise SurfaceError(404, "surface %r has no region %r"
                               % (surface["id"], named))
        if not region["touch"] or not _contains(region["rect"], x, y):
            raise SurfaceError(409, "point is not in touch region %r" % named)
    else:
        region = region_at(surface, x, y)
        if region is None:
            raise SurfaceError(409, "no touch region at (%s, %s)" % (x, y))
    event = {"surface": surface["id"], "region": region["name"],
             "interaction": kind, "points": points, "x": x, "y": y,
             "local": {"x": x - region["rect"]["x"], "y": y - region["rect"]["y"]}}
    for key in NUMERIC_EXTRAS:
        if key in body:
            event[key] = _num(body[key], key)
    return event
