"""Merged-feature navigation: GLANCE/AIM mode and app-strip page.

Single concept: the two header controls with static bodies -- pinning the
mode (keeping the window start) and stepping the app strip one page (keeping
the mode). Both re-show through show() and refuse unless the merged feature is
showing; a miss is a 409, never a view change.
"""

from renderer_registry import macbook_layout_module


class MacbookModeMixin:
    """GLANCE/AIM mode and strip page controls."""

    # ---- Merged-feature navigation (GLANCE/AIM + tab page) --------------
    # The header controls are closed touch actions with static bodies
    # (macbook_mode pins the mode, talon_tab pins the page direction);
    # the daemon applies them to the showing macbook view, preserving the
    # other param, so a mode switch never loses the window start and a
    # tab page never leaves the mode. Both re-show through show() (manual
    # navigation: holds rotation, cancels transients), and both refuse
    # unless the merged feature is showing -- misses are 409, never a
    # view change.

    def _macbook_mode(self):
        """Showing mode: 'aim' or 'glance' (default). Never raises."""
        try:
            if self.current != "macbook":
                return "glance"
            params = self.current_params or {}
            return "aim" if str(params.get("mode") or "").lower() \
                == "aim" else "glance"
        except Exception:
            return "glance"

    def _macbook_tab(self):
        """Showing tab index: int >= 0, default 0. Never raises."""
        try:
            if self.current != "macbook":
                return 0
            return max(0, int((self.current_params or {}).get("tab", 0)))
        except (TypeError, ValueError):
            return 0
        except Exception:
            return 0

    def request_mode_move(self, mode):
        """Re-show the merged feature in `mode`, keeping the tab.

        Returns {"ok": True, "mode", "tab"} or {"ok": False,
        "reason"}. Raises ValueError only for a bad mode name."""
        if self.current != "macbook":
            return {"ok": False,
                    "reason": "macbook view not showing "
                    "(showing %r)" % (self.current,)}
        try:
            want = str(mode or "").lower()
        except Exception:
            want = ""
        if want not in ("glance", "aim"):
            raise ValueError("macbook mode must be glance or aim")
        params = dict(self.current_params or {})
        params["mode"] = want
        # A mode switch is not a page turn: drop any stale slide hint
        # so the fresh thread draws steady instead of replaying it.
        params.pop("tab_from", None)
        self.show("macbook", params)
        return {"ok": True, "mode": want,
                "tab": params.get("tab", 0)}

    def request_tab_step(self, direction):
        """Page the header app strip one window, clamped at both ends.

        One press moves the visible window by PAGE_STRIDE (VISIBLE - 1,
        so the new window overlaps the old by one chip) with a rapid
        slide; a press at either end is refused (ok False) and that
        stepper draws dim. Returns {"ok": True, "tab", "tab_from"}
        or {"ok": False, "reason"}. Raises ValueError only for a
        bad direction."""
        if self.current != "macbook":
            return {"ok": False,
                    "reason": "macbook view not showing "
                    "(showing %r)" % (self.current,)}
        if self._macbook_mode() != "glance":
            return {"ok": False,
                    "reason": "app strip lives in GLANCE mode"}
        if isinstance(direction, bool):
            raise ValueError("tab direction must be +1 or -1")
        try:
            direction = int(direction)
        except (TypeError, ValueError):
            raise ValueError("tab direction must be an integer")
        if abs(direction) != 1:
            raise ValueError("tab direction must be +1 or -1")
        if macbook_layout_module is None:
            return {"ok": False,
                    "reason": "app-list geometry unavailable"}
        state = self._talon_apps_state()
        if state is None:
            return {"ok": False,
                    "reason": "no fresh talon_apps feed "
                    "(poller quiet >%ds?)" % (self.FOCUS_FRESH,)}
        apps = state.get("apps")
        if not isinstance(apps, list) or not apps:
            return {"ok": False, "reason": "no running apps in feed"}
        params = dict(self.current_params or {})
        old = macbook_layout_module.page_start(
            params.get("tab", 0), len(apps))
        new, moved = macbook_layout_module.page(
            old, direction, len(apps))
        if not moved:
            edge = "first" if direction < 0 else "last"
            return {"ok": False,
                    "reason": "already at %s page (%d app%s)" %
                    (edge, len(apps),
                     "" if len(apps) == 1 else "s")}
        params["tab"] = new
        # Slide hint for the fresh renderer thread: it restarts on the
        # new window, so it needs the old one to animate old-to-new.
        params["tab_from"] = old
        self.show("macbook", params)
        return {"ok": True, "tab": new, "tab_from": old}
