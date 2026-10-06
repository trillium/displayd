"""The Talon app-focus slot: panel tap -> Mac app focus.

Single concept: one TTL-bounded pending focus command, the same shape as the
cursor slot. The queued command names the app only from the latest feed -- the
HTTP body carries coordinates, never a name -- so a tap can only ever select a
listed app.
"""

import time

from renderer_registry import macbook_layout_module, talon_apps_module


class FocusMixin:
    """Talon focus command queue."""

    # ---- Talon app-focus slot ------------------------------------------
    # Panel tap -> Mac focus, same shape as the macbook-mouse slot
    # (PR #10): POST /talon/focus queues ONE TTL command carrying a
    # panel point; the Mac-side poller (bridges/talon_apps.py) fetches it
    # via GET /talon/focus?since= and hands the app name to Talon. The
    # queued command names the app ONLY from the latest feed -- the HTTP
    # body carries coordinates, never a name -- so a tap can only ever
    # select a listed app, never an arbitrary target. Failures refuse,
    # never half-fire; stale commands TTL-expire instead of firing late.
    FOCUS_TTL = 10.0  # pending commands older than this never run
    FOCUS_FRESH = 5.0  # talon_apps feed must be this fresh to map against

    def _talon_apps_state(self):
        """Latest talon_apps feed state, or None when absent/stale."""
        try:
            values = self.feeds.get("talon_apps", "state")
        except Exception:
            return None
        state = values[-1] if values else None
        if not isinstance(state, dict):
            return None
        try:
            age = time.time() - float(state.get("ts", 0))
        except (TypeError, ValueError):
            return None
        if age < 0 or age > self.FOCUS_FRESH:
            return None
        return state

    def request_focus_move(self, px, py):
        """Queue a focus change for panel pixel (px, py).

        Returns {"ok": True, "command": {...}} on success, or
        {"ok": False, "reason": ...} on any refusal (wrong view or
        mode, stale feed, tap outside the app chips). Raises ValueError
        only for malformed coordinates (non-int or off-panel)."""
        for value in (px, py):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    "talon focus coordinates must be integers")
        width, height = self.screen.W, self.screen.H
        if not (0 <= px < width and 0 <= py < height):
            raise ValueError(
                "talon focus coordinates off-panel: %r,%r "
                "for %dx%d" % (px, py, width, height))
        if self.current != "macbook":
            return {"ok": False,
                    "reason": "macbook view not showing "
                    "(showing %r)" % (self.current,)}
        if self._macbook_mode() != "glance":
            return {"ok": False,
                    "reason": "app strip lives in GLANCE mode"}
        if talon_apps_module is None or macbook_layout_module is None:
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
        tab = self._macbook_tab()
        index = macbook_layout_module.chip_hit(px, py, width, apps, tab)
        if index is None:
            return {"ok": False,
                    "reason": "tap outside the app chips"}
        name = talon_apps_module.clean(apps[index])
        if not name:
            return {"ok": False,
                    "reason": "tap outside the app chips"}
        command = {"app": name, "index": index, "ts": time.time()}
        with self.lock:
            self.focus_seq = getattr(self, "focus_seq", 0) + 1
            command["id"] = self.focus_seq
            self.focus_pending = command
        with self.cmd_cond:
            self.cmd_cond.notify_all()
        self.policy.note_api()
        return {"ok": True, "command": dict(command)}

    def take_focus_move(self, since=None):
        """Pending focus command newer than `since`, else None.

        Read-only: the poller tracks the last ts it acted on, so a
        retried fetch never double-fires and a crashed-then-restarted
        poller skips TTL-expired commands instead of replaying them."""
        try:
            since = float(since) if since is not None else 0.0
        except (TypeError, ValueError):
            since = 0.0
        with self.lock:
            pending = getattr(self, "focus_pending", None)
            pending = dict(pending) if pending else None
        if pending is None:
            return None
        if pending["ts"] <= since:
            return None
        if time.time() - pending["ts"] > self.FOCUS_TTL:
            return None
        return pending
