"""A surface's declarative ``layout``: geometry in, explicit regions out.

Single concept: a registered surface may say *how* its usable viewport is
partitioned (stack, grid, or rect, the vocabulary ``POST /layout`` takes)
instead of computing pixel rects itself. ``source`` turns that directive into
the explicit ``regions`` list ``surface_model`` already validates, using the
one geometry engine in ``layout_geometry``; the rects are computed inside
``viewport.usable`` and offset by its origin. Each entry names a surface
region (``name``, optional ``content_types``/``touch``) rather than a renderer.
Pure; raises ``ValueError`` naming the ``layout`` field.
"""

import copy

import layout_geometry as geometry

PASSTHROUGH = ("name", "content_types", "touch")


def source(spec, usable, limit):
    """``(raw regions, field name)`` for a spec: its ``layout`` derived, or
    its own ``regions`` untouched. Both given is refused."""
    layout = spec.get("layout")
    if layout is None:
        return spec.get("regions"), "regions"
    if spec.get("regions") is not None:
        raise ValueError("layout: give either 'layout' or 'regions', not both")
    return derive(layout, usable, limit), "layout.regions"


def derive(layout, usable, limit):
    """Explicit region items (rect dicts in viewport pixels) for ``layout``."""
    if not isinstance(layout, dict):
        raise ValueError("layout: expected an object")
    items = layout.get("regions")
    if not isinstance(items, list) or not items:
        raise ValueError("layout.regions: expected a non-empty list")
    if len(items) > limit:
        raise ValueError("layout.regions: at most %d" % limit)
    bound = []
    for i, item in enumerate(items):
        where = "layout.regions[%d]" % i
        if not isinstance(item, dict):
            raise ValueError("%s: expected an object" % where)
        if "renderer" in item or "params" in item:
            raise ValueError("%s: a surface region names no renderer" % where)
        bound.append({"_spec": item, "_where": where})
    geometry.partition(layout, bound, usable["w"], usable["h"])
    out = []
    for i, (item, entry) in enumerate(zip(items, bound)):
        x, y, w, h = entry["rect"]
        region = {k: item[k] for k in PASSTHROUGH if k in item}
        region["rect"] = {"x": usable["x"] + x, "y": usable["y"] + y,
                          "w": w, "h": h}
        for j, other in enumerate(out):
            if _overlap(region["rect"], other["rect"]):
                raise ValueError("layout.regions[%d]: overlaps layout.regions"
                                 "[%d]" % (i, j))
        out.append(region)
    return out


def _overlap(a, b):
    return (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
            and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])


def stored(spec):
    """``{"layout": copy}`` for the record, or ``{}`` when none was given."""
    layout = spec.get("layout")
    return {} if layout is None else {"layout": copy.deepcopy(layout)}
