"""Screen power, the idle watchdog, and the policy surface.

Single concept: the daemon's autonomous behaviour actuators -- the validated
policy config surface, the idle-off decision and its watchdog thread, and
power on/off with the pre-sleep return target. The decisions themselves are
policy.py; this is the wiring that acts on them.
"""

import copy
import threading
import os

SLEEP_VIEW = "sleep"
POLICY_FILE = os.environ.get(
    "DISPLAYD_POLICY",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "policy.json"),
)


class PowerMixin:
    """Policy surface, idle watchdog, and screen power."""

    # ---- policy configuration surface ------------------------------------

    def get_policy(self):
        return {
            "config": self.policy.get_config(),
            "activity": self.policy.activity_snapshot(),
            "transient": self.policy.transient_status(),
            "idle_off": self.policy.idle_off,
        }

    def set_policy(self, patch):
        config, persisted = self.policy.update_config(patch or {})
        self.policy.note_api()
        self.playlist.config_updated(patch or {})  # saving playlist = run
        state = self.get_policy()
        state["persisted"] = persisted
        return state

    # ---- idle watchdog ------------------------------------------------------

    def check_idle(self):
        """One watchdog tick: blank the panel when the inactivity window
        has elapsed. Public so tests can drive it deterministically."""
        if not self.policy.idle_due():
            return {"idle_off": self.policy.idle_off}
        with self.lock:
            if not self.policy.idle_due() or self.fb.blanked:
                return {"idle_off": self.policy.idle_off}
        # Same sleep view as the manual path (the wake target must work
        # identically), but the rotation keeps running: idle-off is not
        # a manual choice, so it must not hold the playlist.
        self._enter_sleep_view()
        with self.lock:
            if self.fb.blanked:
                return {"idle_off": self.policy.idle_off}
            self.fb.power_off()
            self.policy.idle_off = True
            return {"idle_off": True, "at": self.policy.last_activity()}

    def start_watchdog(self, interval=1.0):
        if self.watchdog_thread is not None:
            return
        self.watchdog_stop.clear()

        def _tick():
            while not self.watchdog_stop.wait(interval):
                try:
                    self.check_idle()
                except Exception:
                    pass

        self.watchdog_thread = threading.Thread(target=_tick, daemon=True)
        self.watchdog_thread.start()

    def stop_watchdog(self):
        self.watchdog_stop.set()
        self.watchdog_thread = None

    # ---- screen power --------------------------------------------------

    def _enter_sleep_view(self):
        """Switch to the dedicated sleep view, remembering the return.

        The return target is the policy base (the pre-transient view),
        falling back to the current view only when no base exists -- a
        transient itself (reload/notice) is never restored, so waking
        after its window still lands somewhere sane. No-op when already
        there, when the sleep renderer is missing, or while a layout
        owns the panel (the layout survives the nap untouched). The
        power calls below do the actual darkening; this only arms the
        touch service's wake signal (renderer == sleep)."""
        if self.layout_state():
            return False
        with self.lock:
            if self.current == SLEEP_VIEW:
                return True
            if ("module" not in
                    (self.renderers.get(SLEEP_VIEW) or {})):
                return False
            base = copy.deepcopy(self.policy.base)
            if base is None and self.current is not None:
                base = {"renderer": self.current,
                        "params": copy.deepcopy(
                            self.current_params or {})}
            if (base is not None
                    and base.get("renderer") == SLEEP_VIEW):
                base = None
        try:
            self._start_view(SLEEP_VIEW, {})
        except (KeyError, ValueError):
            return False
        with self.lock:
            # Re-arm only if the switch actually landed: a concurrent
            # navigation in between owns the panel instead.
            if self.current == SLEEP_VIEW:
                self.sleep_restore = base
        return True

    def _exit_sleep_view(self):
        """Restore the pre-sleep view after power-on. One-shot: the
        slot is consumed whether or not the restore lands (an uninstalled
        renderer must not wedge every later wake). Only acts when the
        sleep view is actually showing -- a plain power-on never yanks
        the current view. With no return pending (slept from a blank
        panel, or a manual demo), falls back to the clock, mirroring
        the transient-expiry fallback: waking must always land somewhere
        navigable, never on a touch-sticky lit sleep view. Callers paint
        this while still dark so the first photons are the base view."""
        with self.lock:
            restore = self.sleep_restore
            self.sleep_restore = None
            waking = (self.current == SLEEP_VIEW)
        if not waking:
            return False
        try:
            if restore:
                self._start_view(restore["renderer"],
                                 restore.get("params") or {})
            else:
                self._start_view("clock", {})
        except (KeyError, ValueError):
            return False
        return True

    def set_power(self, power):
        power = (power or "").lower()
        if power not in ("on", "off"):
            raise ValueError("power must be 'on' or 'off'")
        self.policy.note_api()
        if power == "off":
            # Sleep is a manual choice like /show: hold the rotation so
            # no invisible frames advance it mid-nap, then switch to the
            # sleep view (arming the touch wake target) before darkening.
            self.playlist.on_manual()
            self._enter_sleep_view()
            with self.lock:
                # A manual power call means the operator owns the power
                # state: it clears the watchdog's idle_off claim.
                self.policy.idle_off = False
                result = self.fb.power_off()
        else:
            # Restore first (still dark), then light up: power_on
            # repaints the last frame, which is the base view again.
            self._exit_sleep_view()
            with self.lock:
                self.policy.idle_off = False
                result = self.fb.power_on()
        state = self.state()
        state["applied"] = result
        return state
