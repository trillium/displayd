"""Read-only tap resolution against the current UI.

Single concept: answering "what would a tap at this point do right now?" --
the same view/mode-gated region + named-action resolution the touch service
performs, with the coordinate parsing helper for POST /touch/resolve.
Dispatches nothing and changes no state.
"""

from daemon_touch_audit import touch_audit


def _resolve_coords(body, width, height):
    """POST /touch/resolve coordinates -> (x, y) display pixels.

    Accepts display pixels {x, y} (ints) or normalized {x_norm, y_norm}
    floats in [0, 1] (same edge mapping as touch.normalize: 0 -> 0,
    1 -> size - 1). Explicit pixels win when both forms are present.
    Pure: raises ValueError naming the defect, changes nothing."""
    body = body if isinstance(body, dict) else {}
    if body.get("x") is not None or body.get("y") is not None:
        if body.get("x") is None or body.get("y") is None:
            raise ValueError("resolve needs both {x, y} pixels")
        return body.get("x"), body.get("y")
    xn, yn = body.get("x_norm"), body.get("y_norm")
    if xn is None or yn is None:
        raise ValueError("resolve needs {x, y} pixels or "
                         "{x_norm, y_norm} fractions")
    for value in (xn, yn):
        if isinstance(value, bool) or \
                not isinstance(value, (int, float)):
            raise ValueError("resolve fractions must be numbers")
        if not 0.0 <= value <= 1.0:
            raise ValueError("resolve fractions range 0..1, "
                             "got %r,%r" % (xn, yn))
    return int(round(xn * (width - 1))), int(round(yn * (height - 1)))


class TouchResolveMixin:
    """Read-only tap resolution."""

    # Tap-positioned actions (touch.py COORD_ACTIONS): the region names
    # the action, the tap point positions it. Resolve stamps the point
    # like the touch path, but feed/map revalidation stays at dispatch
    # (request_mouse/click/focus_move) -- out of scope for this step.
    RESOLVE_COORD_ACTIONS = ("talon_focus", "macbook_mouse",
                               "macbook_click")

    def resolve_touch(self, x, y):
        """Read-only tap resolution against the CURRENT UI.

        Takes display-pixel ints, topmost-hit-tests them against the
        announced live set (touch_audit.candidates: view-specific first,
        then global -- the same order the touch service dispatches), and
        returns what the tap WOULD do: the resolved region id and the
        semantic action. Dispatches nothing, queues nothing, notes no
        activity, changes no state: a question, not a command.

        The refusal gates mirror the real path's predicates exactly:
        unknown (no heartbeat) answers ok False like touch_check; a tap
        while a layout owns the panel sees global regions only (layout
        clears current, so the touch service never guesses a scope);
        view+mode-gated actions (macbook_mouse only in GLANCE,
        macbook_click only in AIM, talon_focus/talon_tab only in GLANCE,
        macbook_mode only while macbook shows, reload_confirm only while
        a reload transient is active) report refused with the same reason
        strings the dispatching handlers use, EXCEPT the macbook mode
        gates: a region whose action the showing mode refuses is not live
        (AIM never draws the map or the strip), so the tap falls through
        to the next region exactly like the touch dispatcher
        (touch_audit.candidates_for_mode) -- the second tap reaches the
        click catcher instead of reporting the map refused; a dead-zone tap while a
        reload transient is active reports consumed_by reload-dismiss
        (dismissal-consumes-tap precedence: the real tap would return
        the panel, never navigate). Raises ValueError on malformed or
        off-panel coordinates (HTTP 400)."""
        for value in (x, y):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    "resolve coordinates must be integers")
        width, height = self.screen.W, self.screen.H
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError(
                "resolve coordinates off-panel: %r,%r "
                "for %dx%d" % (x, y, width, height))
        if touch_audit is None:
            return {"ok": False, "status": "error",
                    "error": "touch audit helper unavailable"}
        live = self.touch_live
        if live is None:
            return {"ok": False, "status": "unknown",
                    "error": "no touch heartbeat: restart displayd-touch "
                    "(it announces at startup) or run touch.py --announce"}
        view = self.current  # None while a layout owns the panel
        layout = self.layout_state() is not None
        announced = live.get("announced") or {}
        scoped = announced.get("view_regions") or {}
        global_regions = announced.get("regions") or []
        found = None
        for region in touch_audit.candidates_for_mode(
                global_regions, scoped, view, self._macbook_mode()):
            rect = region.get("rect")
            if not rect or len(rect) != 4:
                continue
            rx, ry, rw, rh = rect
            if rx <= x < rx + rw and ry <= y < ry + rh:
                found = region
                break
        base = {"ok": True, "view": view, "layout": layout,
                "x": x, "y": y}
        active = getattr(self.policy, "active", None)
        reload_active = (isinstance(active, dict)
                         and active.get("kind") == "reload")
        if found is None:
            base.update({
                "hit": False, "region": None, "action": None,
                "dispatched": False,
                "consumed_by": ("reload-dismiss" if reload_active
                                  else None),
                "fallback": ("touch-service tap_options "
                               "(daemon-side unknown)"),
            })
            return base
        rid = found.get("id")
        action = dict(found.get("action") or {})
        name = action.get("name")
        if name in self.RESOLVE_COORD_ACTIONS:
            # The region names it, the tap positions it (touch.py
            # handle_frame stamps the same way before dispatch).
            action["x"], action["y"] = x, y
        reason = self._resolve_refusal(name, view)
        base.update({"hit": True, "region": rid, "action": action,
                     "dispatched": False})
        if reason is not None:
            base.update({"refused": True, "reason": reason})
        else:
            base.update({"refused": False})
            if name in self.RESOLVE_COORD_ACTIONS:
                base["revalidation"] = (
                    "daemon feed/map revalidation still applies at "
                    "dispatch (request_mouse/click/focus_move)")
        return base

    def _resolve_refusal(self, name, view):
        """View/mode-gate predicate for one action name, read-only.

        Returns the refusal reason the dispatching handler would use,
        or None when no view/mode gate refuses. Reason strings match
        the handlers so diffs compare equal. Feed/freshness/map gates
        are NOT evaluated here (tap-positioned daemon revalidation)."""
        showing = "(showing %r)" % (view,)
        if name == "reload_confirm":
            active = getattr(self.policy, "active", None)
            if not (isinstance(active, dict)
                    and active.get("kind") == "reload"):
                return "no-reload-active"
            return None
        if name == "macbook_mouse":
            if view != "macbook":
                return "macbook view not showing " + showing
            if self._macbook_mode() != "glance":
                return ("map lives in GLANCE mode "
                        "(open AIM to review, not to position)")
            return None
        if name == "macbook_click":
            if view != "macbook":
                return "macbook view not showing " + showing
            if self._macbook_mode() != "aim":
                return ("review image lives in AIM mode "
                        "(position in GLANCE first)")
            return None
        if name in ("talon_focus", "talon_tab"):
            if view != "macbook":
                return "macbook view not showing " + showing
            if self._macbook_mode() != "glance":
                return "app strip lives in GLANCE mode"
            return None
        if name == "macbook_mode":
            if view != "macbook":
                return "macbook view not showing " + showing
            return None
        return None
