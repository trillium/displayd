"""The Mardi Gras bucket model: how an issue lands in a parade bucket.

Single concept: the four-bucket classification every beads view shares --
Rolling / Lined Up / Stalled / Past the Stand -- with Stalled DERIVED from
dependency edges rather than read from any status field, plus the reverse
edges (dependents), the normalized snapshot build, and the attention list of
what needs the captain. One source of truth so the overview and the detail
card cannot drift. Pure: no I/O, no state.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from beads_issue import _norm_issue

# Dependency edge types where "A --type--> B" means A waits on B.
WAITS_ON = {"blocks", "blocked-by", "depends-on"}

# Labels that flag an open bead as needing the captain directly.
CAPTAIN_LABELS = {"human", "captain-hold", "captain", "gate"}

# Ceiling on the NEEDS THE CAPTAIN rows the overview can show.
ATTENTION_ROWS = 6


def _classify(pairs):
    by_id, bare = {}, {}
    for store, raw in pairs:
        norm = _norm_issue(store, raw)
        if norm is not None:
            by_id[(norm["store"], norm["id"])] = norm
            bare.setdefault(norm["id"], norm)
    snap = {"rolling": [], "linedup": [], "stalled": [], "past": [],
            "attention": [], "stores": {}, "total": 0, "closed": 0,
            "index": by_id, "bare": bare}
    for issue in by_id.values():  # one entry per (store, id)
        store_stat = snap["stores"].setdefault(
            issue["store"], {"open": 0, "total": 0})
        store_stat["total"] += 1
        snap["total"] += 1
        status = issue["status"]
        if status == "closed":
            snap["past"].append(issue)
            snap["closed"] += 1
            continue
        # Blocked is DERIVED from dependency edges: an unfinished bead this
        # one waits on. Never read from a status field.
        waiters = _waiters(issue, by_id, bare)
        if waiters:
            issue["waiters"] = waiters
            snap["stalled"].append(issue)
            store_stat["open"] += 1
        elif status == "in_progress":
            snap["rolling"].append(issue)
            store_stat["open"] += 1
        else:  # open, deferred, anything else unfinished: nothing in the way
            snap["linedup"].append(issue)
            store_stat["open"] += 1
    snap["attention"] = _attention(snap)
    return snap


def _waiters(issue, by_id, bare):
    out = []
    for dep in issue["deps"]:
        if dep["type"] not in WAITS_ON:
            continue
        target = by_id.get((issue["store"], dep["target"])) or bare.get(dep["target"])
        if target is not None and target["status"] != "closed":
            out.append(target)
    return out


def bucket_of(issue, snap):
    """Which parade bucket an issue is in, plus the waiters that put it
    there. Single source of truth for overview and detail alike."""
    if issue["status"] == "closed":
        return "past", []
    waiters = _waiters(issue, snap.get("index") or {}, snap.get("bare") or {})
    if waiters:
        return "stalled", waiters
    if issue["status"] == "in_progress":
        return "rolling", []
    return "linedup", []


def dependents_of(issue, snap):
    """Beads waiting on this one (reverse edges), unfinished first."""
    out = []
    for other in (snap.get("index") or {}).values():
        if other is issue:
            continue
        for dep in other["deps"]:
            if dep["type"] not in WAITS_ON:
                continue
            if dep["target"] == issue["id"]:
                out.append(other)
                break
    out.sort(key=lambda i: (i["status"] == "closed", i["priority"], i["id"]))
    return out


def _attention(snap):
    """What needs the captain: review queue, stalled work, open decisions."""
    picked, seen = [], set()

    def take(issue, reason):
        if issue["id"] not in seen:
            seen.add(issue["id"])
            picked.append({"issue": issue, "reason": reason})

    review = [i for i in snap["linedup"] + snap["stalled"]
              if i["store"] == "review"]
    review.sort(key=lambda i: (i["priority"], i["id"]))
    for issue in review[:3]:
        take(issue, "review queue")
    stalled = sorted(snap["stalled"], key=lambda i: (i["priority"], i["id"]))
    for issue in stalled:
        if len(picked) >= ATTENTION_ROWS:
            break
        first = (issue.get("waiters") or [{}])[0]
        take(issue, "waits on %s" % (first.get("title") or first.get("id") or "?"))
    gates = [i for i in snap["linedup"] + snap["rolling"]
             if i["labels"] & CAPTAIN_LABELS
             or i["title"].lower().startswith(("gate:", "decide:", "decision:"))]
    gates.sort(key=lambda i: (i["priority"], i["id"]))
    for issue in gates:
        if len(picked) >= ATTENTION_ROWS:
            break
        take(issue, "needs a captain call")
    return picked[:ATTENTION_ROWS]


def find_in_snapshot(snap, bead_id):
    """Resolve a bead id against the snapshot index. Exact id wins; a
    unique prefix or suffix match is accepted for wall use."""
    if not snap or not bead_id:
        return None
    bare = snap.get("bare") or {}
    if bead_id in bare:
        return bare[bead_id]
    cands = [i for bid, i in bare.items()
             if bid.startswith(bead_id) or bid.endswith(bead_id)]
    if len(cands) == 1:
        return cands[0]
    return None
