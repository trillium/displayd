"""Columns: a stream shown in a region of a surface, by a named view.

Single concept: the *composition* layer between the three things that are
deliberately kept apart --

* an **event stream** (surface_streams.py): what happens;
* a **surface** / **region** (surface_model.py): where it can be shown;
* a **column**: ``{"id", "stream", "view", "region", "options"}``, the binding
  "show stream S in region R using view V". Several columns compose on one
  surface (one per region, left to right in list order); the same stream may
  appear in columns on many surfaces; a surface can change a column without
  the stream or any other column noticing.

Columns live in the surface's panel config (``panel.config.columns``, see
surface_panel.py). ``view`` is just a token naming a UI module the renderer
implementation understands ("log", "calls", "chart", "controls"): displayd
never draws it, it only checks it against what the stream says it offers.

A column's *follow-on UI* is the stream's declared ``actions``: ``act`` turns
a viewer's choice in that column into a validated action queued for the
stream's producer.
"""

from surface_model import ID_RE, SurfaceError, _dict, _token, default_region_name

MAX_COLUMNS = 12
MAX_OPTIONS_BYTES = 2048


def normalize_columns(raw, regions):
    """Validate a column list against the surface's regions; returns the
    stored shape (region filled in) or raises ``ValueError``."""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)) or len(raw) > MAX_COLUMNS:
        raise ValueError("columns: expected a list of at most %d" % MAX_COLUMNS)
    names = [r["name"] for r in regions]
    out, ids, used = [], set(), set()
    for i, item in enumerate(raw):
        where = "columns[%d]" % i
        item = _dict(item, where)
        cid = _token(item.get("id"), where + ".id")
        region = item.get("region") or default_region_name(regions)
        if region is None:
            raise ValueError("%s.region: the surface has several regions: name one"
                             % where)
        if region not in names:
            raise ValueError("%s.region: surface has no region %r" % (where, region))
        options = _dict(item.get("options") or {}, where + ".options")
        if len(repr(options)) > MAX_OPTIONS_BYTES:
            raise ValueError("%s.options: over %d bytes" % (where, MAX_OPTIONS_BYTES))
        if cid in ids or region in used:
            raise ValueError("%s: duplicate column id or region (one column per "
                             "region)" % where)
        ids.add(cid)
        used.add(region)
        out.append({"id": cid, "stream": _token(item.get("stream"), where + ".stream",
                                                ID_RE),
                    "view": _token(item.get("view"), where + ".view"),
                    "region": region, "options": options})
    return out


def columns_of(surface):
    return (surface.get("panel") or {}).get("config", {}).get("columns", [])


def find(surface, column_id):
    for column in columns_of(surface):
        if column["id"] == column_id:
            return column
    raise SurfaceError(404, "surface %r has no column %r"
                       % (surface["id"], column_id))


def describe(surface, streams):
    """The surface's columns with what its streams currently offer, so a
    Sidecar can subscribe and render without a second lookup."""
    out = []
    for column in columns_of(surface):
        stream = streams.find(column["stream"])
        views = stream["views"] if stream else []
        out.append(dict(column, stream_info=stream,
                        view_supported=bool(stream) and (not views or
                                                         column["view"] in views)))
    return {"surface": surface["id"], "panel": surface["panel"]["id"],
            "columns": out}


def act(registry, streams, sid, column_id, body):
    """A viewer's follow-on action in one column -> the stream's producer."""
    body = _dict(body, "action")
    surface = registry.get(sid)
    column = find(surface, column_id)
    event_seq = body.get("event_seq")
    if event_seq is not None and (isinstance(event_seq, bool)
                                  or not isinstance(event_seq, int)):
        raise ValueError("event_seq: expected an integer")
    stored = streams.add_action(column["stream"], {
        "action": _token(body.get("action"), "action"), "column": column["id"],
        "surface": sid, "panel": surface["panel"]["id"], "region": column["region"],
        "event_seq": event_seq, "value": body.get("value")})
    return {"ok": True, "action": stored}
