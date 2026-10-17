"""Daemon wiring for the review decision page.

Single concept: connecting ``review_flow`` (the state machine) to the panel:
the ``review`` view shows the flow's variables, every transition re-feeds
them, and a confirmed decision leaves as an event on the ``review-decisions``
stream (surface_streams.py), which the Mac-side ``bridges/review_bridge.py``
executes against the review store. The daemon never touches the store.

Taps (``review_select`` / ``review_confirm`` / ``review_back``) and the API
share these methods. Like the macbook controls they are view-gated: a miss
(the review view is not showing) is a 409 and changes nothing.
"""

import threading

import review_decision
import review_summary
import review_view
from review_flow import ReviewFlow
from surface_model import SurfaceError

VIEW = "review"
STREAM = "review-decisions"
RETURN_VIEW = "clock"


class ReviewMixin:
    """Review flow state plus the panel actions that drive it."""

    _review_lock = threading.Lock()

    @property
    def review_flow(self):
        flow = self.__dict__.get("_review_flow")
        if flow is None:
            with self._review_lock:
                flow = self.__dict__.get("_review_flow")
                if flow is None:
                    flow = self._review_flow = ReviewFlow(self._review_publish)
                    self._review_prior = None
        return flow

    def _review_streams(self):
        """The hub's streams with ``review-decisions`` declared (upsert), so a
        bridge that starts after a load always finds it."""
        streams = self.surface_hub.streams
        streams.declare({"id": STREAM, "name": "Review decisions",
                         "event_types": ["decision"]})
        return streams

    def _review_publish(self, event):
        self._review_streams().publish(STREAM, {"type": "decision", "data": event})

    def _review_push(self):
        """Re-draw the page for the flow's current state (only while shown)."""
        if self.current == VIEW:
            self.feed(VIEW, "vars", review_view.build_vars(self.review_flow.snapshot()))

    def _review_gate(self):
        if self.current != VIEW:
            raise SurfaceError(409, "review view not showing (showing %r)" % (self.current,))

    def review_state(self):
        snap = self.review_flow.snapshot()
        return dict(snap, vars=review_view.build_vars(snap), showing=self.current == VIEW)

    def review_load(self, bead):
        """Parse a bead, load it and show the page (ValueError on a bad bead)."""
        decision = review_summary.apply(review_decision.parse_bead(bead))
        self._review_streams()
        snap = self.review_flow.load(decision)
        if self.current != VIEW:
            self._review_prior = (self.current or RETURN_VIEW, dict(self.current_params or {}))
        self.show(VIEW, {"bead": decision["id"], "vars": review_view.build_vars(snap)})
        return self.review_state()

    def review_select(self, slot):
        self._review_gate()
        self.review_flow.select(slot)
        self._review_push()
        return self.review_state()

    def review_confirm(self):
        self._review_gate()
        self.review_flow.confirm()
        self._review_push()
        return self.review_state()

    def review_back(self):
        """BACK: one step toward the options; from the options (or a finished
        send) it leaves the page for the view that was showing before."""
        self._review_gate()
        flow = self.review_flow
        if flow.snapshot()["state"] in ("confirming", "failed"):
            flow.back()
            self._review_push()
        else:
            flow.close()
            name, params = getattr(self, "_review_prior", None) or (RETURN_VIEW, {})
            self.show(name if name != VIEW else RETURN_VIEW, params)
        return self.review_state()

    def review_result(self, nonce, ok, message=""):
        """The executor's answer; allowed whether or not the page is showing."""
        self.review_flow.result(nonce, bool(ok), message)
        self._review_push()
        return self.review_state()
