"""Static-region layout parsing: geometry in, pixel rects out.

Single concept: turning a /layout request's region specs -- stack, grid, or an
explicit rect -- into pixel rects, atomically: one bad spec rejects the whole
request and nothing is applied. Pure: no framebuffer, no daemon. Capability
fit (a full-panel-only view cannot land in a reduced region) is checked here
too, through capability.py, so the rule has one home.
"""

import capability
from schema import validate_params

MAX_LAYOUT_REGIONS = 16


def _parse_dim(value, total, where, allow_zero=False):
    """Parse one region dimension: px int, \"NNpx\", or \"NN%\" of total.
    Returns int px. Raises ValueError on anything unusable."""
    if isinstance(value, bool):
        raise ValueError("%s: must be a pixel size or percentage" % where)
    if isinstance(value, (int, float)):
        px = int(value)
        if px < (0 if allow_zero else 1):
            raise ValueError("%s: must be at least %dpx" %
                             (where, 0 if allow_zero else 1))
        return px
    text = str(value).strip().lower()
    if text.endswith("%"):
        try:
            frac = float(text[:-1].strip()) / 100.0
        except ValueError:
            raise ValueError("%s: bad percentage %r" % (where, value))
        if not 0 < frac <= 1:
            raise ValueError("%s: percentage must be within (0, 100]" % where)
        return max(1, int(round(total * frac)))
    if text.endswith("px"):
        text = text[:-2].strip()
    try:
        px = int(float(text))
    except ValueError:
        raise ValueError("%s: bad size %r" % (where, value))
    if px < 1:
        raise ValueError("%s: must be at least 1px" % where)
    return px


def _grid_rect(row, col, row_span, col_span, rows, cols, width, height):
    """One grid cell as absolute px, tiling the screen edge to edge."""
    x = col * width // cols
    w = (col + col_span) * width // cols - x
    y = row * height // rows
    h = (row + row_span) * height // rows - y
    return (x, y, max(1, w), max(1, h))


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

    grid_mode = (payload.get("rows") is not None
                 or payload.get("cols") is not None
                 or any("row" in r["_spec"] or "col" in r["_spec"]
                          for r in bound))
    rect_mode = not grid_mode and any(isinstance(r["_spec"].get("rect"),
                                                 (dict, list, tuple))
                                      for r in bound)
    if grid_mode:
        _assign_grid(payload, bound, width, height)
    elif rect_mode:
        for r in bound:
            r["rect"] = _assign_rect(r["_spec"], r["_where"], width, height)
    else:
        _assign_stack(bound, width, height)

    for r in bound:
        x, y, w, h = r["rect"]
        # Clip to the panel; a region reduced to nothing is a bad layout.
        x = max(0, min(x, width - 1))
        y = max(0, min(y, height - 1))
        w = max(1, min(w, width - x))
        h = max(1, min(h, height - y))
        r["rect"] = (x, y, w, h)
        del r["_spec"]
        del r["_where"]
    capability.check_fit(bound, renderers, width, height)
    return bound


def _assign_grid(payload, bound, width, height):
    """Tile regions over a rows x cols grid. Missing row/col auto-fills
    free cells in order; spans default to 1."""
    spans = []
    for r in bound:
        spec, where = r["_spec"], r["_where"]
        try:
            rs = int(spec.get("row_span", spec.get("rowspan", 1)))
            cs = int(spec.get("col_span", spec.get("colspan", 1)))
        except (TypeError, ValueError):
            raise ValueError("%s: spans must be integers" % where)
        if rs < 1 or cs < 1:
            raise ValueError("%s: spans must be at least 1" % where)
        spans.append((rs, cs))
    rows = payload.get("rows")
    cols = payload.get("cols")
    try:
        rows = int(rows) if rows is not None else None
        cols = int(cols) if cols is not None else None
    except (TypeError, ValueError):
        raise ValueError("'rows'/'cols' must be integers")
    need_rows = 0
    need_cols = 0
    for r, (rs, cs) in zip(bound, spans):
        spec = r["_spec"]
        if "row" in spec:
            need_rows = max(need_rows, int(spec["row"]) + rs)
        if "col" in spec:
            need_cols = max(need_cols, int(spec["col"]) + cs)
    if rows is None:
        rows = max(need_rows, 1)
    if cols is None:
        cols = max(need_cols, 1)
    if rows < 1 or cols < 1:
        raise ValueError("'rows'/'cols' must be at least 1")
    if rows * cols > MAX_LAYOUT_REGIONS * 4:
        raise ValueError("'rows' x 'cols' grid is too large")
    taken = set()
    auto = 0
    for r, (rs, cs) in zip(bound, spans):
        spec, where = r["_spec"], r["_where"]
        if "row" in spec or "col" in spec:
            try:
                row = int(spec.get("row", 0))
                col = int(spec.get("col", 0))
            except (TypeError, ValueError):
                raise ValueError("%s: row/col must be integers" % where)
        else:
            while auto in taken:
                auto += 1
            row, col = auto // cols, auto % cols
        if not (0 <= row < rows) or not (0 <= col < cols):
            raise ValueError("%s: row/col outside the %dx%d grid"
                             % (where, rows, cols))
        if row + rs > rows or col + cs > cols:
            raise ValueError("%s: span overflows the %dx%d grid"
                             % (where, rows, cols))
        for rr in range(row, row + rs):
            for cc in range(col, col + cs):
                if (rr, cc) in taken:
                    raise ValueError("%s: overlaps another region" % where)
                taken.add((rr, cc))
        auto = row * cols + col + 1
        r["rect"] = _grid_rect(row, col, rs, cs, rows, cols, width, height)


def _assign_rect(spec, where, width, height):
    """Explicit rect: [x, y, w, h] or {x, y, w/width, h/height}."""
    raw = spec.get("rect")
    if isinstance(raw, (list, tuple)):
        if len(raw) != 4:
            raise ValueError("%s.rect: want [x, y, w, h]" % where)
        x, y, w, h = raw
    elif isinstance(raw, dict):
        x = raw.get("x", 0)
        y = raw.get("y", 0)
        w = raw.get("w", raw.get("width"))
        h = raw.get("h", raw.get("height"))
        if w is None or h is None:
            raise ValueError("%s.rect: w and h are required" % where)
    else:
        raise ValueError("%s.rect: must be an object or [x, y, w, h]" % where)
    return (_parse_dim(x, width, where + ".rect.x", allow_zero=True),
            _parse_dim(y, height, where + ".rect.y", allow_zero=True),
            _parse_dim(w, width, where + ".rect.w"),
            _parse_dim(h, height, where + ".rect.h"))


def _assign_stack(bound, width, height):
    """Simple split: heights stack vertically (full width); widths alone
    stack horizontally (full height). Omitted sizes share evenly."""
    horizontal = (any("width" in r["_spec"] for r in bound)
                  and not any("height" in r["_spec"] for r in bound))
    if horizontal:
        share = width // len(bound)
        cursor = 0
        for k, r in enumerate(bound):
            spec, where = r["_spec"], r["_where"]
            last = (k == len(bound) - 1)
            w = (width - cursor) if last and "width" not in spec else \
                _parse_dim(spec.get("width", share), width, where + ".width")
            r["rect"] = (cursor, 0, w, height)
            cursor += w
    else:
        share = height // len(bound)
        cursor = 0
        for k, r in enumerate(bound):
            spec, where = r["_spec"], r["_where"]
            last = (k == len(bound) - 1)
            h = (height - cursor) if last and "height" not in spec else \
                _parse_dim(spec.get("height", share), height,
                           where + ".height")
            r["rect"] = (0, cursor, width, h)
            cursor += h
