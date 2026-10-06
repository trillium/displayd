"""The live and drawn region sets of one view (which region is live).

Single concept: answering "which regions are live for the showing view" --
loading a renderer module, the drawn geometry it projects for the audit
(expected_for_view), the candidate list for a view (view-specific first,
then global), and the macbook mode gate that filters the regions an
undrawn mode would refuse. Pure except for the renderer imports; the wire
format lives in touch_audit_wire.py.
"""

import importlib.util
import os

def _load_renderer(name):
    """Import a module under renderers/ by path (lazy: stdlib-only
    import). ``name`` may be a slashed subpath, so a component-layer
    module ("ui/system_buttons") loads the same way a view does."""
    rel = name.replace(".", "/").strip("/")
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "renderers", *rel.split("/")) + ".py"
    spec = importlib.util.spec_from_file_location(
        "touch_audit_" + rel.replace("/", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def candidates(regions, view_regions, view):
    """Candidates for one view: view-specific FIRST, then global."""
    scoped = (view_regions or {}).get(view) or [] if view else []
    return [dict(r) for r in list(scoped) + list(regions or [])]


# Mode-gated touch actions (mirror of DisplayDaemon._resolve_refusal in
# displayd.py): the macbook scope mixes GLANCE-only regions (map, app
# strip) with the AIM-only fullscreen click catcher, so first-hit-wins
# alone cannot tell them apart -- in AIM every tap re-hits the earlier
# map region and re-fires (then refuses) the warp instead of clicking.
# Both the touch dispatcher and the read-only resolver skip regions
# whose action the showing mode would refuse, so the second tap falls
# through to the click catcher. Off-view and reload gates are NOT
# skipped (those regions stay live and report refused); only the
# macbook mode gates filter, because an undrawn mode's regions are not
# live -- AIM draws the fullscreen capture, never the map or the strip.
MODE_GLANCE_ACTIONS = ("macbook_mouse", "talon_focus", "talon_tab")
MODE_AIM_ACTIONS = ("macbook_click",)


def mode_live(action_name, view, mode):
    """True when a region naming `action_name` is live under (view,
    mode). Only the macbook mode gates filter; every other action is
    live wherever its scope is. Unknown mode defaults to glance, exactly
    like DisplayDaemon._macbook_mode."""
    if view != "macbook":
        return True
    if mode == "aim":
        return action_name not in MODE_GLANCE_ACTIONS
    return action_name not in MODE_AIM_ACTIONS


def candidates_for_mode(regions, view_regions, view, mode):
    """Mode-aware candidates: candidates() minus regions whose action
    the showing mode refuses. Same order, same shapes."""
    out = []
    for region in candidates(regions, view_regions, view):
        action = region.get("action")
        name = action.get("name") if isinstance(action, dict) else None
        if mode_live(name, view, mode):
            out.append(region)
    return out


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
    exact, presence, note = [], [], ""
    if view in ("picker", "unified"):
        try:
            mod = _load_renderer(view)
            views = (list(picker_views) if picker_views is not None
                     else mod.coerce_views(params))
            if view == "unified":
                exact = mod.audit_exact(w, h, views, params)
            else:
                exact = [{"id": e["id"],
                          "rect": [int(v) for v in e["rect"]],
                          "action": e["action"], "required": True}
                         for e in mod.picker_regions(
                              w, h, views, mod.coerce_rect(params, w, h))]
            note = " + %d tile(s)" % len(exact)
        except Exception as exc:
            return {"view": view, "checkable": False,
                    "reason": "%s geometry failed: %s" % (view, exc),
                    "exact": [], "presence": []}
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
    gated = {"macbook": ["macbook_mouse", "macbook_click", "talon_focus",
                        "macbook_mode", "talon_tab"],
             "sleep": ["screen_on"]}
    if view in gated:  # view-gated actions: unwired taps die silent
        for action in gated[view]:
            presence.append({"action": action})
        note += " + " + view
    try:
        exact.extend(_load_renderer("ui/system_buttons").audit_exact(
            view, w, h))
    except Exception as exc:
        return {"view": view, "checkable": False,
                "reason": "system-button geometry failed: %s" % exc,
                "exact": [], "presence": []}
    return {"view": view, "checkable": True,
            "reason": ("badges" + note).strip(),
            "exact": exact, "presence": presence}
