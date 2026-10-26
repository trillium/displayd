"""Targeting: how a consumer names a surface and/or a region.

Single concept: turning a consumer's intent into concrete
``(surface, region)`` pairs, and the discovery filter that shares its
vocabulary. Three target forms, all accepted anywhere a target is:

* ``"ipad"`` / ``"ipad/left"`` -- a surface id, optionally ``/region``.
* ``{"surface": "ipad", "region": "left"}`` -- the same, structured; or
  ``{"surface": "ipad", "column": "tools"}`` -- the region that column of the
  surface's panel config is bound to (columns are not regions: see
  surface_columns.py).
* ``{"select": {...}, "region": "left", "all": false}`` -- by capability:
  the first match (by id) or, with ``all``, every match.

A ``select`` query (also the ``GET /surfaces?...`` filter) understands:
``kind``, ``renderer`` (name), ``content_type``, ``touch`` (an interaction
name, ``any`` or ``none``), ``min_width`` / ``min_height`` (usable pixels),
``region`` (must be declared), ``labels`` (``{"room": "studio"}``), ``panel``
(the logical panel id) and ``stream`` (shows a column of that event stream).
Every key must hold; an unknown key is refused rather than ignored, so a
typo cannot silently widen a broadcast.
"""

from surface_columns import columns_of, find
from surface_model import (TOUCH_INTERACTIONS, SurfaceError,
                           default_region_name)

QUERY_KEYS = ("kind", "renderer", "content_type", "touch", "min_width",
              "min_height", "region", "labels", "panel", "stream")


def check_query(query):
    """Validate a select query; returns it normalized or raises ValueError."""
    if not isinstance(query, dict):
        raise ValueError("select: expected an object")
    for key in query:
        if key not in QUERY_KEYS:
            raise ValueError("select: unknown key %r (one of %s)"
                             % (key, ", ".join(QUERY_KEYS)))
    touch = query.get("touch")
    if touch is not None and touch not in TOUCH_INTERACTIONS + ("any", "none"):
        raise ValueError("select.touch: expected an interaction, 'any' or 'none'")
    for key in ("min_width", "min_height"):
        if key in query and (isinstance(query[key], bool)
                             or not isinstance(query[key], int)):
            raise ValueError("select.%s: expected an integer" % key)
    labels = query.get("labels")
    if labels is not None and not (isinstance(labels, dict) and all(
            isinstance(v, str) for v in labels.values())):
        raise ValueError("select.labels: expected an object of strings")
    return query


def match(surface, query):
    """Does this surface satisfy every key of the query?"""
    if not query:
        return True
    usable = surface["viewport"]["usable"]
    touch = surface["touch"]
    region = query.get("region")
    checks = (
        ("kind" not in query or surface["kind"] == query["kind"]),
        ("renderer" not in query
         or surface["renderer"]["name"] == query["renderer"]),
        ("content_type" not in query
         or query["content_type"] in surface["renderer"]["content_types"]),
        ("min_width" not in query or usable["w"] >= query["min_width"]),
        ("min_height" not in query or usable["h"] >= query["min_height"]),
        (region is None or any(r["name"] == region for r in surface["regions"])),
        ("panel" not in query or surface["panel"]["id"] == query["panel"]),
        ("stream" not in query or any(c["stream"] == query["stream"]
                                      for c in columns_of(surface))),
        all(surface["labels"].get(k) == v
            for k, v in (query.get("labels") or {}).items()),
    )
    if not all(checks):
        return False
    want = query.get("touch")
    if want is None:
        return True
    if want == "none":
        return not touch["supported"]
    if want == "any":
        return touch["supported"]
    return want in touch["interactions"]


def parse_target(target):
    """A target in any accepted form -> ``{surface, region, select, all}``."""
    if isinstance(target, str):
        surface, _, region = target.partition("/")
        if not surface or "/" in region:
            raise ValueError("target: expected 'surface' or 'surface/region'")
        return {"surface": surface, "region": region or None, "column": None,
                "select": None, "all": False}
    if not isinstance(target, dict):
        raise ValueError("target: expected a string or an object")
    surface, select = target.get("surface"), target.get("select")
    if (surface is None) == (select is None):
        raise ValueError("target: give exactly one of 'surface' or 'select'")
    region = target.get("region")
    if region is not None and not isinstance(region, str):
        raise ValueError("target.region: expected a string")
    if surface is not None and not isinstance(surface, str):
        raise ValueError("target.surface: expected a string")
    column = target.get("column")
    if column is not None and (not isinstance(column, str) or region is not None
                               or surface is None):
        raise ValueError("target.column: a string, with 'surface' and no 'region'")
    if select is not None:
        select = dict(check_query(select))
    return {"surface": surface, "region": region, "column": column,
            "select": select, "all": bool(target.get("all"))}


def region_of(surface, name):
    """The named region of a surface (``None`` name = its default)."""
    name = name or surface["default_region"]
    if name is None:
        raise SurfaceError(409, "surface %r has several regions and no 'full': "
                                "name one" % surface["id"])
    for region in surface["regions"]:
        if region["name"] == name:
            return region
    raise SurfaceError(404, "surface %r has no region %r" % (surface["id"], name))


def resolve(registry, target, content_type=None):
    """Concrete ``[(surface, region)]`` for a target; never empty.

    ``content_type`` (what is about to be sent) lets a *first-match* select
    pick the first surface that can take it instead of one that cannot; a
    broadcast keeps every match, so the hub can report the skips.

    A named surface that is missing or expired is a ``SurfaceError`` (404 /
    410) via the registry; a select that matches nothing is a 404.
    """
    parsed = parse_target(target)
    if parsed["surface"] is not None:
        surface = registry.get(parsed["surface"])
        if parsed["column"]:
            return [(surface, region_of(surface, find(surface, parsed["column"])["region"]))]
        return [(surface, region_of(surface, parsed["region"]))]
    query = dict(parsed["select"])
    if parsed["region"] and "region" not in query:
        query["region"] = parsed["region"]
    if content_type and not parsed["all"]:
        query.setdefault("content_type", content_type)
    found = registry.list(query)
    if not found:
        raise SurfaceError(404, "no live surface matches %r" % parsed["select"])
    if not parsed["all"]:
        # newest registration wins; built-ins (no registered_at) keep first
        # place and equal stamps fall back to id, so the pick is stable
        found = [min(found, key=lambda s: (
            s["registered_at"] is not None, -(s["registered_at"] or 0), s["id"]))]
    pairs = []
    for surface in found:
        name = parsed["region"] or default_region_name(surface["regions"])
        if name is None:
            continue  # ambiguous here; a broadcast skips it, never guesses
        pairs.append((surface, region_of(surface, name)))
    if not pairs:
        raise SurfaceError(409, "matched surfaces have several regions and no "
                                "'full': name one")
    return pairs
