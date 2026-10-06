"""The MacBook second-tap click slot: review image -> one CG click.

Single concept: one TTL-bounded pending click at the REVIEWED point. The tap
only proves it landed on the review image; the click target is the capture's
own crosshair, gated on a fresh state feed, a fresh zoom capture that
post-dates the positioning tap, and a cursor still on the point. Every miss is
a refusal, never a blind click.
"""

import time


class ClickMixin:
    """MacBook click command queue."""

    # ---- MacBook second-tap click slot -----------------------------------
    # Stage 2 of the two-stage tap (stage 1 = POST /macbook/mouse moves
    # the cursor ONLY, then the Mac posts a magnified /feed/macbook/zoom
    # capture around it). POST /macbook/click queues ONE TTL click at
    # the REVIEWED point; the Mac-side poller fetches it via GET
    # /macbook/click?since= and posts one CG down+up pair. The tap
    # point only proves the tap landed on the review image (pane
    # membership) -- the click target is the capture's own crosshair
    # point, never a re-mapping of the tap, so a tap cannot drift off
    # the reviewed pixel. Commit gates (every miss is a 409 refusal,
    # never a click): fresh state feed, fresh zoom capture (proves the
    # review surface is current), the capture post-dates the
    # positioning tap, and the live cursor still sits on the point (a
    # first tap plus a later second tap never clicks where the mouse
    # has since moved). No arming, no double-click -- each POST queues
    # at most one command and the poller acts once per ts (take_*
    # stays read-only, like the rest).
    CLICK_TTL = 15.0  # pending clicks older than this never run
    CLICK_FRESH = 30.0  # zoom capture must be this fresh to click against
    CLICK_EPS = 8.0  # Quartz-px tolerance: capture vs cursor

    def _macbook_zoom_state(self):
        """Latest macbook zoom capture, or None when absent/stale."""
        try:
            values = self.feeds.get("macbook", "zoom")
        except Exception:
            return None
        zoom = values[-1] if values else None
        if not isinstance(zoom, dict):
            return None
        try:
            age = time.time() - float(zoom.get("ts", 0))
        except (TypeError, ValueError):
            return None
        if age < 0 or age > self.CLICK_FRESH:
            return None
        return zoom

    @staticmethod
    def _near(ax, ay, bx, by, eps):
        try:
            return abs(float(ax) - float(bx)) <= eps and \
                abs(float(ay) - float(by)) <= eps
        except (TypeError, ValueError):
            return False

    def request_click_move(self, px, py):
        """Queue a click for panel pixel (px, py).

        Returns {"ok": True, "command": {...}} or {"ok": False,
        "reason": ...}. Raises ValueError only for malformed
        coordinates (non-int or off-panel)."""
        for value in (px, py):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    "macbook click coordinates must be integers")
        width, height = self.screen.W, self.screen.H
        if not (0 <= px < width and 0 <= py < height):
            raise ValueError(
                "macbook click coordinates off-panel: %r,%r "
                "for %dx%d" % (px, py, width, height))
        if self.current != "macbook":
            return {"ok": False,
                    "reason": "macbook view not showing "
                    "(showing %r)" % (self.current,)}
        if self._macbook_mode() != "aim":
            return {"ok": False,
                    "reason": "review image lives in AIM mode "
                    "(position in GLANCE first)"}
        # AIM claims the full canvas: every on-panel tap is on the review
        # image (the header controls route first via touch order, and
        # direct POSTs already passed the bounds check above), so no
        # sub-pane check remains.
        state = self._macbook_map_state()
        if state is None:
            return {"ok": False,
                    "reason": "no fresh macbook feed "
                    "(poller quiet >%ds?)" % (self.MOUSE_FRESH,)}
        zoom = self._macbook_zoom_state()
        if zoom is None:
            return {"ok": False,
                    "reason": "no fresh review capture "
                    "(position first, then tap the image)"}
        try:
            qx, qy = float(zoom["x"]), float(zoom["y"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False,
                    "reason": "review capture has no point "
                    "(position again)"}
        mouse = state.get("mouse") or {}
        if not self._near(qx, qy, mouse.get("x"),
                           mouse.get("y"), self.CLICK_EPS):
            return {"ok": False,
                    "reason": "cursor moved since positioning "
                    "(position again, then tap the image)"}
        with self.lock:
            pending = getattr(self, "mouse_pending", None)
            pending = dict(pending) if pending else None
        if pending is not None:
            try:
                if float(zoom.get("ts", 0)) < float(pending["ts"]):
                    return {"ok": False,
                            "reason": "review capture predates the "
                            "last positioning (wait for the new image)"}
            except (TypeError, ValueError):
                pass
        try:
            display_index = int((mouse or {}).get("display_index", 0))
        except (TypeError, ValueError):
            display_index = 0
        command = {"x": qx, "y": qy,
                   "display_index": display_index,
                   "ts": time.time()}
        with self.lock:
            self.click_seq = getattr(self, "click_seq", 0) + 1
            command["id"] = self.click_seq
            self.click_pending = command
        with self.cmd_cond:
            self.cmd_cond.notify_all()
        self.policy.note_api()
        return {"ok": True, "command": dict(command)}

    def take_click_move(self, since=None):
        """Pending click command newer than `since`, else None.

        Read-only: the poller tracks the last ts it acted on, so a
        retried fetch never double-fires and a crashed-then-restarted
        poller skips TTL-expired commands instead of replaying them."""
        try:
            since = float(since) if since is not None else 0.0
        except (TypeError, ValueError):
            since = 0.0
        with self.lock:
            pending = getattr(self, "click_pending", None)
            pending = dict(pending) if pending else None
        if pending is None:
            return None
        if pending["ts"] <= since:
            return None
        if time.time() - pending["ts"] > self.CLICK_TTL:
            return None
        return pending
