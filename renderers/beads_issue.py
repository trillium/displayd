"""Normalizing one raw store issue into the beads view model.

Single concept: turning a raw store issue dict (as exported by a store CLI
or written to a mirror file) into the one normalized shape every beads view
reads -- labels, the last few comments, dependency edges, and the display
fields, with missing or malformed values coerced instead of raising. Pure:
no I/O, no state. Fetching lives in beads_source.py; classification lives
in beads_buckets.py.
"""


def _as_labels(raw):
    out = set()
    for item in raw or []:
        name = item.get("name") if isinstance(item, dict) else item
        if name:
            out.add(str(name).strip().lower())
    return out


def _trim_comment(raw):
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or "").strip()
    if not text:
        return None
    return {
        "author": str(raw.get("author") or "?"),
        "text": text[:220],
        "created_at": raw.get("created_at"),
    }


def _norm_issue(store, raw):
    """Trim a store issue dict to what the views need. Tolerates mirrors
    that predate the detail fields (they arrive as None)."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    deps = []
    for dep in raw.get("dependencies") or []:
        if not isinstance(dep, dict):
            continue
        target = dep.get("depends_on_id") or dep.get("target") or dep.get("id")
        dtype = str(dep.get("type") or "").strip().lower()
        if target and dtype:
            deps.append({"type": dtype, "target": str(target)})
    try:
        prio = int(raw.get("priority") or 0)
    except (TypeError, ValueError):
        prio = 0
    try:
        ccount = int(raw.get("comment_count") or 0)
    except (TypeError, ValueError):
        ccount = 0
    comments = []
    for c in raw.get("comments") or []:
        t = _trim_comment(c)
        if t is not None:
            comments.append(t)
    comments = comments[-4:]  # latest few earn wall space; history does not
    desc = raw.get("description")
    return {
        "store": store,
        "id": str(raw.get("id")),
        "title": str(raw.get("title") or "(untitled)"),
        "status": str(raw.get("status") or "open").strip().lower(),
        "priority": prio,
        "issue_type": str(raw.get("issue_type") or "task"),
        "labels": _as_labels(raw.get("labels")),
        "deps": deps,
        "description": str(desc)[:1500] if desc else "",
        "owner": raw.get("owner"),
        "assignee": raw.get("assignee"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
        "close_reason": raw.get("close_reason"),
        "comment_count": ccount,
        "comments": comments,
    }
