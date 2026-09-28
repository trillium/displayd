"""Drawn-vs-live touch region assertion: DRAWN renderer geometry vs the
LIVE region set announced by the running touch service (POST
/touch/announce). File-vs-file diffs miss this drift (files agree while
the service dispatches its startup set). Reachable as GET /touch/check
(matrix over every view, gated in deploy.sh). Per-view regions:
global entries live everywhere, view_regions.<view> only while that
view shows -- global sets cannot separate two view-specific areas
sharing screen space (picker tiles vs the macbook map). No input-loop
reload: freshness via unit restart."""
import hashlib
import importlib.util
import json
import os

SUPPRESSED_CHROME = ("picker", "reload", "notice")
COVERAGE = ("exact(id+rect+action): picker tiles, retro-grid cells; "
            "exact(id+rect): home badge; presence-only: macbook_mouse "
            "on macbook, talon_focus on talon_apps; reported-not-asserted: "
            "strips, reload_confirm, feedback; unasserted: layout, blank")


def _load_renderer(name):
    """Import renderers/<name>.py by path (lazy: stdlib-only import)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "renderers", name + ".py")
    spec = importlib.util.spec_from_file_location("touch_audit_" + name,
                                                  path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def candidates(regions, view_regions, view):
    """Candidates for one view: view-specific FIRST, then global."""
    scoped = (view_regions or {}).get(view) or [] if view else []
    return [dict(r) for r in list(scoped) + list(regions or [])]


def expected_for_view(view, params=None, w=1920, h=1080,
                      picker_views=None):
    """Drawn geometry for one view (rects asserted; actions when set)."""
    params = params if isinstance(params, dict) else {}
    try:
        w, h = int(w), int(h)
        assert w > 0 and h > 0
    except (TypeError, ValueError, AssertionError):
        return {"view": view, "checkable": False,
                "reason": "bad display size", "exact": [], "presence": []}
    if not view:
        return {"view": view, "checkable": True, "reason": "panel blank",
                "exact": [], "presence": []}
    if view == "picker":
        try:
            pk = _load_renderer("picker")
            views = (list(picker_views) if picker_views is not None
                     else pk.coerce_views(params))
            exact = [{"id": e["id"], "rect": [int(v) for v in e["rect"]],
                      "action": e["action"], "required": True}
                     for e in pk.picker_regions(
                          w, h, views, pk.coerce_rect(params, w, h))]
        except Exception as exc:
            return {"view": view, "checkable": False,
                    "reason": "picker geometry failed: %s" % exc,
                    "exact": [], "presence": []}
        return {"view": view, "checkable": True,
                "reason": "%d tile(s)" % len(exact),
                "exact": exact, "presence": []}
    exact, presence, note = [], [], ""
    if view == "retro_grid":
        try:
            rg = _load_renderer("retro_grid")
            cols = params.get("columns", 4)
            rows = params.get("rows", 3)
            cols = cols if isinstance(cols, int) and 1 <= cols <= 12 else 4
            rows = rows if isinstance(rows, int) and 1 <= rows <= 12 else 3
            gut = params.get("gutter")
            gut = gut if isinstance(gut, int) and gut >= 0 else None
            for e in rg.touch_regions(w, h, cols, rows, gut):
                # Rects only: cell labels live in action titles, which are
                # operator styling, not drawn geometry.
                exact.append({"id": e["id"],
                              "rect": [int(v) for v in e["rect"]],
                              "action": None, "required": False})
            note = " + %d cell(s)" % len(exact)
        except Exception as exc:
            return {"view": view, "checkable": False,
                    "reason": "retro-grid geometry failed: %s" % exc,
                    "exact": [], "presence": []}
    gated = {"macbook": "macbook_mouse", "talon_apps": "talon_focus"}
    if view in gated:  # view-gated coord actions: unwired taps die silent
        presence.append({"action": gated[view]})
        note += " + " + view
    if view not in SUPPRESSED_CHROME:
        try:
            badge = _load_renderer("home_chrome").home_region(w, h)
            exact.append({"id": badge["id"],
                          "rect": [int(v) for v in badge["rect"]],
                          "action": None, "required": True})
        except Exception as exc:
            return {"view": view, "checkable": False,
                    "reason": "home-badge geometry failed: %s" % exc,
                    "exact": [], "presence": []}
    else:
        note += ": dismissal is server-side, no badge drawn"
    return {"view": view, "checkable": True,
            "reason": ("home badge" + note).strip(),
            "exact": exact, "presence": presence}


def compare_exact(expected_exact, live, force_strict=False):
    """Exact entries vs live. Undrawn live ids reported, never failed.
    A drawn id with no live region fails only when required (or the view
    is claimed via force_strict); unclaimed opt-in geometry (retro cells
    with no scope) lands in unwired instead of failing the matrix."""
    missing, moved, unwired = [], [], []
    for entry in expected_exact or []:
        got = live.get(entry["id"])
        if got is None:
            bucket = (missing if entry.get("required", True)
                      or force_strict else unwired)
            bucket.append({"id": entry["id"],
                           "expected_rect": entry["rect"],
                           "expected_action": entry["action"]})
            continue
        changed = (entry["action"] is not None
                   and got["action"] != entry["action"])
        if got["rect"] != entry["rect"] or changed:
            moved.append({"id": entry["id"],
                          "expected_rect": entry["rect"],
                          "live_rect": got["rect"],
                          "action_changed": changed,
                          "expected_action": entry["action"],
                          "live_action": got["action"]})
    wanted = {e["id"] for e in expected_exact or []}
    return {"ok": not missing and not moved, "missing": missing,
            "moved": moved, "unwired": unwired,
            "unasserted": sorted(set(live) - wanted)}


def compare_presence(presence, live):
    """Required actions vs live (rects are operator-chosen)."""
    have = {(info.get("action") or {}).get("name") for info in live.values()}
    missing = [p for p in presence or [] if p.get("action") not in have]
    return {"ok": not missing, "missing": missing}


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
