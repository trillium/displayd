"""The announce heartbeat wire format for the touch region audit.

Single concept: the shape of the region set the touch service announces and
the daemon checks -- byte-level parsing and canonicalization of live regions
([x, y, w, h] coercion), the stable id/rect/action sha both sides compare,
and validation of an announce body (unique ids across the global and every
scoped set, with the defect named). Nothing here reads a renderer or a
view; the geometry side lives in touch_audit_regions.py.
"""

import hashlib
import json

def _int4(rect):
    """[x, y, w, h] numbers (bool excluded) -> [int x4], else None."""
    ok = (isinstance(rect, (list, tuple)) and len(rect) == 4 and all(
        isinstance(v, (int, float)) and not isinstance(v, bool)
        for v in rect))
    return [int(v) for v in rect] if ok else None


def normalize_live(regions):
    """Region list -> {id: {"rect", "action"}}; skips garbage (the
    announce validator rejects it at the door; this stays total)."""
    out = {}
    for region in regions or []:
        if not isinstance(region, dict):
            continue
        rid, rect, action = (region.get("id"), _int4(region.get("rect")),
                             region.get("action"))
        if not rid or not isinstance(rid, str) or rect is None:
            continue
        out[rid] = {"rect": rect,
                    "action": action if isinstance(action, dict) else {}}
    return out


def regions_sha(announced):
    """Stable sha256 over global + scoped id/rect/action projection."""
    if isinstance(announced, list):
        announced = {"regions": announced}
    body = announced if isinstance(announced, dict) else {}
    canon = {"global": _canon(normalize_live(body.get("regions"))),
             "scoped": {v: _canon(normalize_live(r)) for v, r in
                        sorted((body.get("view_regions") or {}).items())}}
    blob = json.dumps(canon, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _canon(norm):
    return sorted(({"id": rid, **info} for rid, info in norm.items()),
                  key=lambda entry: entry["id"])


def _clean_region(region, seen, where):
    if not isinstance(region, dict):
        raise ValueError("%s: region must be an object" % where)
    rid = region.get("id")
    if not rid or not isinstance(rid, str):
        raise ValueError("%s: region needs a string id" % where)
    if rid in seen:
        raise ValueError("duplicate region id %r" % (rid,))
    seen.add(rid)
    rect = _int4(region.get("rect"))
    if rect is None:
        raise ValueError("region %r needs rect [x, y, w, h]" % (rid,))
    action = region.get("action")
    if not isinstance(action, dict) or not action.get("name"):
        raise ValueError("region %r needs an action with a name" % (rid,))
    return {"id": rid, "rect": rect, "action": dict(action)}


def validate_announce(body):
    """Heartbeat body -> {"regions", "view_regions"} cleaned. Ids unique
    across global AND every scoped set. Raises ValueError, names defect."""
    if not isinstance(body, dict):
        raise ValueError("announce body must be a JSON object")
    regions = body.get("regions")
    scoped_in = body.get("view_regions")
    if regions is None:
        regions = []
    if scoped_in is None:
        scoped_in = {}
    if not isinstance(regions, list) or not isinstance(scoped_in, dict):
        raise ValueError("announce regions must be a list, "
                         "view_regions an object")
    seen = set()
    cleaned = [_clean_region(r, seen, "regions") for r in regions]
    scoped = {}
    for view, entries in scoped_in.items():
        if not view or not isinstance(view, str) or "/" in view:
            raise ValueError("view_regions needs plain view names")
        if not isinstance(entries, list) or not entries:
            raise ValueError("view_regions[%r] needs a non-empty list"
                             % (view,))
        scoped[view] = [_clean_region(r, seen, "scoped:%s" % view)
                        for r in entries]
    if not cleaned and not scoped:
        raise ValueError("announce needs at least one region")
    return {"regions": cleaned, "view_regions": scoped}


def _slim(entries):
    return [{"id": r.get("id"), "rect": list(r.get("rect") or []),
             "action": dict(r.get("action") or {})}
            for r in entries or []]


def announce_payload(config):
    """Touch config -> heartbeat body (global + scoped regions + sha)."""
    config = config or {}
    body = {"regions": _slim(config.get("regions")),
            "view_regions": {view: _slim(entries) for view, entries in
                               (config.get("view_regions") or {}).items()}}
    body["regions_sha"] = regions_sha(body)
    return body
