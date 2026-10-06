"""Transients: temporarily show something else, then go back.

Single concept: the switch-away/return mechanism shared by notices and reload
confirmations -- arming the return timer, expiring it, waking an idled panel
first, and the notice itself (validated severity, bounded duration).
"""

import threading

from schema import validate_params


class TransientMixin:
    """Transient switch/return plus notify."""

    def _cancel_transient_timer(self):
        timer, self.transient_timer = self.transient_timer, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _arm_transient(self, kind, token, duration):
        self._cancel_transient_timer()
        timer = threading.Timer(duration, self._transient_expired,
                                  args=(kind, token))
        timer.daemon = True
        self.transient_timer = timer
        timer.start()

    def _transient_expired(self, kind, token):
        """Return timer fired: restore the base view only if nothing
        manual happened since (generation check inside end_transient).
        A reload with no explicit base view (the normal state right
        after a restart) falls back to the clock renderer instead of
        blanking the panel; every other kind keeps the blank return."""
        base, ok = self.policy.end_transient(kind, token)
        if not ok:
            return
        if kind == "reload":
            # The confirmation window is over: the one-time token dies
            # with the view (late scans get 410, never a stale confirm).
            with self.lock:
                if self.reload_token is not None:
                    self.reload_tokens.pop(self.reload_token, None)
                    self.reload_token = None
        try:
            if base is None:
                if kind == "reload":
                    try:
                        self._start_view("clock", {})
                    except (KeyError, ValueError):
                        self._clear_internal()
                else:
                    self._clear_internal()
            else:
                self._start_view(base["renderer"], base["params"])
        except (KeyError, ValueError):
            pass

    def _wake_if_idle(self):
        """Activity arrived while idle-off held the panel dark: power back
        on and repaint. Manual power-off is NOT woken -- the operator owns
        that state; only the watchdog's own power-off auto-wakes.
        Returns True when it woke the panel (the caller then owns the
        sleep-view return -- most callers switch views right after)."""
        if self.policy.idle_off and self.fb.blanked:
            self.fb.power_on()
            self.policy.idle_off = False
            return True
        return False

    SEVERITIES = ("info", "warn", "critical")

    def notify(self, title, body="", severity="info", color=None, duration=None):
        """Show a transient notice, then return to the base view.

        An explicit operator interrupt: while a static-region layout is
        active the notice takes over the full screen (clearing the
        layout) and the return goes to the base view. Chat-attention
        pulls, in contrast, never interrupt a layout -- feeds just update
        the bound region in place.

        Cheap switch-then-return, not composition (ISA D6 option B would
        draw over the current view; this interrupts it instead -- said
        plainly so the captain can decide). Notices preempt chat-attention;
        the return always goes to the base view. Raises ValueError on bad
        input."""
        cfg = self.policy.get_config()["notifications"]
        title = str(title or "").strip()
        if not title:
            raise ValueError("title is required")
        severity = str(severity or "info").lower()
        if severity not in self.SEVERITIES:
            raise ValueError("severity must be one of %s" % "/".join(self.SEVERITIES))
        if duration is None:
            duration = cfg["default_duration"]
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            raise ValueError("duration must be a number of seconds")
        if not (1 <= duration <= 300):
            raise ValueError("duration must be within [1, 300]")
        params = {"title": title, "severity": severity}
        if body:
            params["body"] = str(body)
        if color:
            params["color"] = str(color)
        entry = self.renderers.get("notice")
        if not entry or "module" not in entry:
            raise KeyError("notice renderer is not installed")
        validate_params(params, entry.get("params") or {})
        self.policy.note_api()
        token, superseded = self.policy.begin_transient("notice", duration)
        with self.lock:
            self._arm_transient("notice", token, duration)
            self._wake_if_idle()
            if superseded == "reload" and self.reload_token is not None:
                # The notice took the panel: the reload confirmation
                # window (and its one-time token) dies with the view.
                self.reload_tokens.pop(self.reload_token, None)
                self.reload_token = None
        self._start_view("notice", params)
        out = {"view": "notice", "params": params, "return_in": duration}
        if superseded:
            out["superseded"] = superseded
        return out
