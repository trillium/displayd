"""Turning stored registry records back into live ones.

Single concept: the judgement made on load. Pure functions over plain data
(no clock of its own, no files): ``restore_surfaces`` re-validates every stored
surface through the same ``normalize`` a fresh registration passes -- so a
hand-edited or older record is migrated by the current rules, or dropped and
counted, never half-loaded -- and sorts stale leases into tombstones.
"""

import surface_model as model
from surface_panel import normalize_panel
from surface_streams import normalize_stream

STAMPS = ("registered_at", "updated_at", "last_seen", "expires_at")


def _number(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s: expected a number" % where)
    return float(value)


def restore_surface(rec, now, default_ttl):
    """One stored record -> a live record, or ``ValueError``."""
    spec = model.normalize(rec, default_ttl=default_ttl)
    spec["panel"] = normalize_panel(rec.get("panel"), spec["regions"])
    revision = rec.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("revision: expected a positive integer")
    spec["revision"] = revision
    for key in STAMPS:
        spec[key] = _number(rec.get(key), key)
    return spec


def restore_surfaces(records, tombs, now, default_ttl, reserved=()):
    """``(live, tombs, report)``: ``live`` maps id -> record whose lease still
    holds at ``now``; one that ran out becomes a tombstone at its own
    ``expires_at``, so its owner hears 410 and re-registers."""
    live, out_tombs = {}, {}
    report = {"restored": 0, "expired": 0, "invalid": 0}
    for sid, at in (tombs or {}).items():
        if isinstance(sid, str) and model.ID_RE.match(sid) and \
                isinstance(at, (int, float)) and not isinstance(at, bool):
            out_tombs[sid] = float(at)
    for rec in records:
        try:
            rec = restore_surface(rec, now, default_ttl)
            if rec["id"] in reserved:
                raise ValueError("id: built-in surface id")
        except (ValueError, AttributeError, TypeError):
            report["invalid"] += 1
            continue
        if rec["expires_at"] <= now:
            out_tombs[rec["id"]] = rec["expires_at"]
            report["expired"] += 1
        else:
            live[rec["id"]] = rec
            out_tombs.pop(rec["id"], None)
            report["restored"] += 1
    return live, out_tombs, report


def restore_streams(records):
    """``([declaration, ...], invalid_count)``."""
    out, bad = [], 0
    for rec in records:
        try:
            out.append(normalize_stream(rec))
        except (ValueError, AttributeError, TypeError):
            bad += 1
    return out, bad
