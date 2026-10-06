"""Live view/scope resolution: what the panel shows, and which regions are
therefore live.

Single concept: resolving ``(view, mode)`` from one fresh ``GET /state`` and
selecting the configured regions that scope makes live. ``ScopeMixin`` adds
``current_view``, ``current_scope`` and ``candidate_regions`` to the touch
service; the drawn-vs-live region sets themselves come from touch_audit.
"""

import logging

import touch_audit

LOG = logging.getLogger("displayd-touch")


class ScopeMixin:
    """View/scope resolution for tap routing."""

    def current_view(self):
        """Showing renderer via GET /state, else None. Fresh EVERY tap:
        a cached view goes stale across playlist rotations, phone-driven
        shows, and transient returns -- exactly the drift this closes.
        Unknown (unreachable daemon, blank panel, layout mode) means
        global regions only, never a guess."""
        view, _mode = self.current_scope()
        return view

    def current_scope(self):
        """(view, mode) from one fresh GET /state, else (None, None).

        Mode mirrors DisplayDaemon._macbook_mode exactly: "aim" only
        while the macbook view shows with params mode aim, "glance" on
        the macbook view otherwise, None off-view. The dispatcher needs
        both: the macbook scope mixes GLANCE-only regions with the
        AIM-only click catcher, so the mode decides which are live."""
        fetch = getattr(self.client, "state", None)
        if fetch is None:
            return None, None
        try:
            doc = fetch(timeout=1.0)
        except Exception as exc:
            LOG.warning("touch view fetch failed (global regions only): "
                        "%s", exc)
            return None, None
        if not isinstance(doc, dict):
            return None, None
        view = doc.get("renderer")
        view = view if isinstance(view, str) and view else None
        mode = None
        if view == "macbook":
            mode = "glance"
            params = doc.get("params")
            if isinstance(params, dict):
                raw = params.get("mode")
                if isinstance(raw, str) and raw.lower() == "aim":
                    mode = "aim"
        return view, mode

    def candidate_regions(self, view, mode=None):
        """Live regions for one (view, mode): view-specific FIRST, then
        global (touch_audit.candidates: a view's own area wins its screen
        space while shared chrome still serves), minus regions whose
        action the showing macbook mode would refuse (touch_audit
        .candidates_for_mode: in AIM the map/strip regions are not live,
        so taps fall through to the click catcher; in GLANCE the
        fullscreen click catcher is not live, so header-adjacent taps
        fall through to shared chrome)."""
        return touch_audit.candidates_for_mode(
            self.config.get("regions") or [],
            self.config.get("view_regions") or {}, view, mode)
