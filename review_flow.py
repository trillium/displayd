"""The review decision flow: load -> choose -> confirm -> send -> result.

Single concept: the state machine behind the on-panel decision page. It owns
no I/O: the daemon draws its ``snapshot()`` and hands it a ``publish`` callable
that carries the confirmed decision out to whatever executes it (the Mac-side
bridge, which re-reads the bead and runs the store verb).

States: ``idle`` (nothing loaded), ``showing`` (options listed), ``confirming``
(one option chosen, awaiting an explicit confirm), ``sending`` (published, no
result yet), ``done`` / ``failed`` (the bridge's answer). A tap can only move
the flow along this graph; anything else is a 409 that changes nothing, so a
stale or doubled tap can never send a decision twice.
"""

import threading
import time
import uuid

from surface_model import SurfaceError

SEND_TIMEOUT = 30.0
STATES = ("idle", "showing", "confirming", "sending", "done", "failed")


class ReviewFlow:
    def __init__(self, publish, clock=time.time, send_timeout=SEND_TIMEOUT):
        self._publish = publish
        self._clock = clock
        self._send_timeout = send_timeout
        self._lock = threading.Lock()
        self._state = dict(state="idle", decision=None, chosen=None, nonce=None,
                           message=None, at=None)

    def _expire(self):
        """Liveness: a send nobody answered within the timeout becomes
        ``failed`` (so back/close/retry work). Checked under the lock on every
        read and transition; a late bridge answer is then a 409."""
        st = self._state
        if st["state"] == "sending" and self._clock() - st["at"] >= self._send_timeout:
            self._set(state="failed", message="no answer from bridge")

    def snapshot(self):
        with self._lock:
            self._expire()
            return dict(self._state)

    def _set(self, **change):
        self._state.update(change, at=self._clock())
        return dict(self._state)

    def load(self, decision):
        """Show a parsed decision (``review_decision.parse_bead``). Loading
        again replaces whatever was loaded, even mid-confirm: a new bead is a
        deliberate operator act, not a tap."""
        with self._lock:
            return self._set(state="showing", decision=decision, chosen=None,
                             nonce=None, message=None)

    def _need(self, *states):
        self._expire()
        if self._state["state"] not in states:
            raise SurfaceError(409, "review is %s (needs %s)"
                               % (self._state["state"], " or ".join(states)))

    def select(self, slot):
        """Choose option ``slot`` (1-based) and ask for confirmation."""
        with self._lock:
            self._need("showing")
            decision = self._state["decision"]
            if not decision["actionable"]:
                raise SurfaceError(409, decision["reason"] or "not actionable")
            if isinstance(slot, bool) or not isinstance(slot, int) \
                    or not 1 <= slot <= len(decision["options"]):
                raise SurfaceError(409, "no option %r on this bead" % (slot,))
            return self._set(state="confirming", chosen=decision["options"][slot - 1])

    def confirm(self):
        """Send the chosen option. The event carries a fresh nonce; the
        bridge re-derives the option from the live bead, so the panel never
        names a command, only a bead id and an option id."""
        with self._lock:
            self._need("confirming", "failed")
            decision, chosen = self._state["decision"], self._state["chosen"]
            nonce = uuid.uuid4().hex[:12]
            event = {"nonce": nonce, "bead": decision["id"], "option": chosen["id"],
                     "action": chosen["action"]}
            self._set(state="sending", nonce=nonce, message=None)
        try:
            self._publish(event)
        except Exception as err:
            with self._lock:
                return self._set(state="failed", message="not sent: %s" % err)
        return self.snapshot()

    def back(self):
        """One step back: confirming/failed -> showing. Never undoes a send."""
        with self._lock:
            self._need("confirming", "failed")
            return self._set(state="showing", chosen=None, nonce=None, message=None)

    def close(self):
        """Leave the flow. Refused mid-send (the result is still owed)."""
        with self._lock:
            self._need("showing", "done", "failed", "idle")
            return self._set(state="idle", decision=None, chosen=None, nonce=None,
                             message=None)

    def result(self, nonce, ok, message=""):
        """The executor's answer for the in-flight decision."""
        with self._lock:
            self._need("sending")
            if nonce != self._state["nonce"]:
                raise SurfaceError(409, "result is for another decision")
            return self._set(state="done" if ok else "failed",
                             message=str(message or ("sent" if ok else "failed"))[:300])
