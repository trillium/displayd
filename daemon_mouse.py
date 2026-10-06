"""The MacBook cursor slot: panel tap -> Mac mouse position.

Single concept: one TTL-bounded pending Quartz command. A tap on the macbook
map maps panel pixels through the same geometry the renderer draws, queues the
point for the Mac-side poller, and re-pins the view to AIM review; a miss is a
refusal, never a half-move.
"""

import time

from renderer_registry import macbook_layout_module, macbook_map_module


class MouseMixin:
    """MacBook cursor command queue."""

    # ---- MacBook cursor (panel tap -> Mac mouse) ------------------------
    #
    # A tap while the macbook map view shows moves the MacBook cursor to
    # the tapped point AND enters the fullscreen AIM review of that zone
    # in the same gesture: touch.py posts panel pixels (closed named
    # action `macbook_mouse`); the daemon maps them through the same pure
    # geometry the renderer draws (macbook_map.frame/locate), holds one
    # pending Quartz command the Mac-side poller fetches each tick and
    # warps to directly (Quartz CGWarpMouseCursorPosition -- Talon
    # follows the OS cursor, verified 2026-09-28, so no Talon channel
    # is needed and none is depended on), then re-shows the merged view
    # in AIM mode (tab preserved). The tap is the only entry to the
    # zoom: the retired AIM button is gone, so positioning and review
    # are one gesture, not two. Single fullscreen-map view only: in
    # layout mode the map owns a sub-rect the frame math does not know,
    # so layout taps are refused rather than mis-mapped.
    #
    # Failure is always a refusal, never a half-move: the warp is one
    # atomic OS call on the Mac, so a lost race lands the full point or
    # nothing. Stale commands TTL-expire instead of firing late.
    MOUSE_TTL = 10.0  # pending commands older than this never run
    MOUSE_FRESH = 5.0  # macbook feed must be this fresh to map against

    def _macbook_map_state(self):
        """Latest macbook feed state, or None when absent/stale."""
        try:
            values = self.feeds.get("macbook", "state")
        except Exception:
            return None
        state = values[-1] if values else None
        if not isinstance(state, dict):
            return None
        try:
            age = time.time() - float(state.get("ts", 0))
        except (TypeError, ValueError):
            return None
        if age < 0 or age > self.MOUSE_FRESH:
            return None
        return state

    def request_mouse_move(self, px, py):
        """Queue a cursor move for panel pixel (px, py) and enter AIM.

        Returns {"ok": True, "command": {...}, "mode": "aim",
        "tab": ...} on success -- the warp is queued AND the showing
        view is re-pinned to the fullscreen AIM review (tab preserved),
        so the tap lands the user in the zoom, not back on the glance
        screen. Returns {"ok": False, "reason": ...} on any refusal
        (wrong view, stale feed, tap outside the display map); a
        refusal queues nothing and changes no mode. Raises ValueError
        only for malformed coordinates (non-int or off-panel)."""
        for value in (px, py):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    "macbook mouse coordinates must be integers")
        width, height = self.screen.W, self.screen.H
        if not (0 <= px < width and 0 <= py < height):
            raise ValueError(
                "macbook mouse coordinates off-panel: %r,%r "
                "for %dx%d" % (px, py, width, height))
        if self.current != "macbook":
            return {"ok": False,
                    "reason": "macbook view not showing "
                    "(showing %r)" % (self.current,)}
        if self._macbook_mode() != "glance":
            return {"ok": False,
                    "reason": "map lives in GLANCE mode "
                    "(open AIM to review, not to position)"}
        try:
            macbook_map = macbook_map_module
            if macbook_map is None:
                raise ImportError("macbook_map helper failed to load")
            layout = macbook_layout_module
            if layout is None:
                raise ImportError("macbook_layout helper failed to load")
        except Exception as exc:
            return {"ok": False,
                    "reason": "map geometry unavailable: %s" % (exc,)}
        state = self._macbook_map_state()
        if state is None:
            return {"ok": False,
                    "reason": "no fresh macbook feed "
                    "(poller quiet >%ds?)" % (self.MOUSE_FRESH,)}
        hit = macbook_map.locate(px, py, state.get("displays") or [],
                                 width, height,
                                 top=layout.header_bottom(),
                                 bottom=height)
        if hit is None:
            return {"ok": False,
                    "reason": "tap outside the display map"}
        command = {"x": float(hit["x"]), "y": float(hit["y"]),
                   "display_index": int(hit["display_index"]),
                   "ts": time.time()}
        with self.lock:
            self.mouse_seq = getattr(self, "mouse_seq", 0) + 1
            command["id"] = self.mouse_seq
            self.mouse_pending = command
        with self.cmd_cond:
            self.cmd_cond.notify_all()
        # The tap carries the user into the zoom: re-show the merged
        # feature in AIM mode (manual navigation, so rotation holds
        # while the review is up). Only on success -- a refusal above
        # returns before this line, leaving view and mode untouched.
        params = dict(self.current_params or {})
        params["mode"] = "aim"
        # A mode switch is not a page turn: drop any stale slide hint
        # so the fresh thread draws steady instead of replaying it.
        params.pop("tab_from", None)
        self.show("macbook", params)
        self.policy.note_api()
        return {"ok": True, "command": dict(command),
                "mode": "aim", "tab": params.get("tab", 0)}

    def take_mouse_move(self, since=None):
        """Pending cursor command newer than `since`, else None.

        Read-only: the poller tracks the last ts it acted on, so a
        retried fetch never double-fires and a crashed-then-restarted
        poller skips TTL-expired commands instead of replaying them."""
        try:
            since = float(since) if since is not None else 0.0
        except (TypeError, ValueError):
            since = 0.0
        with self.lock:
            pending = getattr(self, "mouse_pending", None)
            pending = dict(pending) if pending else None
        if pending is None:
            return None
        if pending["ts"] <= since:
            return None
        if time.time() - pending["ts"] > self.MOUSE_TTL:
            return None
        return pending
