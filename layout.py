"""Static-region layout parsing: geometry in, pixel rects out.

Single concept: turning a /layout request's region specs -- stack, grid, or an
explicit rect -- into pixel rects, atomically: one bad spec rejects the whole
request and nothing is applied. Pure: no framebuffer, no daemon. Capability
fit (a full-panel-only view cannot land in a reduced region) is checked here
too, through capability.py, so the rule has one home.
"""

import capability
from layout_geometry import MAX_LAYOUT_REGIONS, partition
from schema import validate_params


def parse_layout(payload, width, height, renderers):
    """Validate a POST /layout body into bound regions.

    Pure: no side effects, so a bad layout is rejected without disturbing
    what is on screen -- the same atomicity guarantee POST /show gives.
    Raises KeyError (unknown renderer) or ValueError (bad shape/geometry).

    Returns a list of {"name", "renderer", "params", "rect": (x, y, w, h)}.

    Geometry, per region (first match wins):
      rect:  {"x","y","w"/"width","h"/"height"} or [x, y, w, h]
             (each px or %; x/w relative to width, y/h to height)
      grid:  row/col (+ row_span/col_span, default 1) tiled over
             top-level rows x cols (inferred when omitted)
      stack: height (vertical split, full width) or width (horizontal
             split, full height); omitted sizes share the screen evenly.
    """
    if not isinstance(payload, dict):
        raise ValueError("layout must be an object")
    regions = payload.get("regions")
    if not isinstance(regions, list) or not regions:
        raise ValueError("'regions' must be a non-empty list")
    if len(regions) > MAX_LAYOUT_REGIONS:
        raise ValueError("at most %d regions" % MAX_LAYOUT_REGIONS)

    names = set()
    bound = []
    for i, region in enumerate(regions):
        where = "regions[%d]" % i
        if not isinstance(region, dict):
            raise ValueError("%s: must be an object" % where)
        name = region.get("name", "region-%d" % i)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("%s: 'name' must be a non-empty string" % where)
        if name in names:
            raise ValueError("duplicate region name %r" % name)
        names.add(name)
        renderer = region.get("renderer")
        if not renderer:
            raise ValueError("%s: 'renderer' is required" % where)
        entry = (renderers or {}).get(renderer)
        if not entry or "module" not in entry:
            raise KeyError("unknown renderer: %s" % renderer)
        params = validate_params(region.get("params"),
                                 entry.get("params") or {})
        bound.append({"name": name, "renderer": renderer,
                      "params": params, "_spec": region, "_where": where})

    partition(payload, bound, width, height)
    for r in bound:
        del r["_spec"]
        del r["_where"]
    capability.check_fit(bound, renderers, width, height)
    return bound
