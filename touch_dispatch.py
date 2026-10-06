"""Tap dispatch: turning a resolved tap into a displayd action.

Single concept: what one tap does. Every valid tap first dismisses an active
reload confirmation (best-effort, and a dismissal that clears a reload
consumes the tap), then hit-tests the live regions and dispatches the
region's named action; an unconsumed tap falls through to the tap-anywhere
options view. Never raises into the service loop.
"""

import logging
import time

from touch_actions import COORD_ACTIONS
from touch_client import TAP_DISMISS_PATH
from touch_regions import hit_test, normalize

LOG = logging.getLogger("displayd-touch")


class TapDispatchMixin:
    """Region hit-testing and safe action dispatch for one tap."""

    def dismiss_reload(self, dry_run=False):
        """Best-effort reload dismissal for one valid tap. Never raises:
        transport failures (and clients without the dismissal method, such
        as older test fakes) are logged and ignored so the configured
        region action that follows always dispatches."""
        dismiss = getattr(self.client, "tap_dismiss", None)
        if dismiss is None:
            legacy = getattr(self.client, "post", None)
            if legacy is None:
                return None
            def dismiss(dry_run=False, _post=legacy):  # noqa: E306
                if dry_run:
                    return {"action": "tap_dismiss", "method": "POST",
                            "path": TAP_DISMISS_PATH, "body": {},
                            "dry_run": True}
                status, resp = _post(TAP_DISMISS_PATH, {})
                return {"action": "tap_dismiss", "status": status,
                        "response": resp}
        try:
            return dismiss(dry_run=dry_run)
        except Exception as exc:
            LOG.warning("reload dismissal failed (region action follows): %s",
                        exc)
            return {"action": "tap_dismiss", "error": str(exc)}

    @staticmethod
    def _dismissal_consumed(dismiss_summary):
        """True when the reload dismissal actually dismissed something.

        The daemon reports {"dismissed": True} only when a reload
        transient was showing; anything else (no-op False, dry-run,
        transport error, legacy client without the method) leaves the tap
        unconsumed for the region/fallback path below."""
        if not isinstance(dismiss_summary, dict):
            return False
        response = dismiss_summary.get("response")
        return (isinstance(response, dict)
                and response.get("dismissed") is True)

    def handle_frame(self, touch_events, dry_run=False):
        """Process one SYN_REPORT frame's TouchEvents. Returns the dispatch
        summary for a tap that hit a region -- or for a dead-zone tap when
        the tap-anywhere fallback (tap_options) is enabled -- else None.

        Every valid tap dismisses an active reload confirmation first
        (POST /touch/tap, best-effort), then hit-tests the configured
        regions as before -- so center/unmatched taps still dismiss reload
        while leaving the region actions unchanged. A dismissal that
        actually clears a reload transient CONSUMES the tap for the
        tap-anywhere fallback only: region hits still dispatch after a
        dismissal, exactly as before, but a dead-zone tap that dismissed
        reload never also navigates to options. Gestures the tap detector rejects
        (swipes, long presses, debounced echoes) never dismiss anything.

        When confidence_feedback is enabled, every *resolved* tap -- region
        hit and dead-zone miss alike -- is reported to the confidence feed
        after tap resolution (and after the configured action, when there
        is one). Invalid gestures (swipes, long presses, incomplete events)
        and debounce-suppressed taps resolve to no tap and emit nothing."""
        now = time.monotonic()
        for event in touch_events:
            event.x, event.y = normalize(
                event.x, event.y, self.config["width"],
                self.config["height"], self.config.get("calibration"))
            tap = self.taps.feed(event, timestamp=now)
            if tap is None:
                continue
            dismissal = self.dismiss_reload(dry_run=dry_run)
            view, mode = self.current_scope()
            candidates = self.candidate_regions(view, mode)
            region_id = hit_test(tap[0], tap[1], candidates)
            if region_id is None:
                if self._dismissal_consumed(dismissal):
                    # Reload-dismiss gesture won: the panel is already on
                    # its normal return path, so the unconsumed-tap
                    # fallback stays out -- the tap was consumed. (Region
                    # hits below still dispatch after a dismissal, exactly
                    # as before: only the new navigation is gated here.)
                    LOG.info("tap at %d,%d dismissed reload transient",
                             tap[0], tap[1])
                    self.send_confidence(
                        self.confidence_payload(
                            tap, None, "tap_dismiss",
                            result="reload-dismissed", view=view),
                        dry_run=dry_run)
                    return dismissal
                LOG.info("tap at %d,%d hit no region", tap[0], tap[1])
                # Dead-zone tap: region hits always win (existing
                # gestures keep working); the unconsumed tap falls
                # through to the options view -- the tap-anywhere
                # fallback. Action first, feedback second, exactly like
                # a region hit: the options dispatch goes out, then the
                # confidence display learns about it (as a dead-zone tap
                # routed to the options action). Returns the options
                # dispatch summary when the fallback fires, else None
                # (fallback disabled).
                if not self.tap_options.get("enabled"):
                    self.send_confidence(
                        self.confidence_payload(tap, None, None,
                                                result="dead-zone",
                                                view=view),
                        dry_run=dry_run)
                    continue
                action = {"name": "options",
                          "renderer": self.tap_options["renderer"],
                          "params": self.tap_options["params"]}
                LOG.info("tap at %d,%d -> options view %r",
                         tap[0], tap[1],
                         self.tap_options["renderer"])
                try:
                    summary = self.client.dispatch(
                        action, dry_run=dry_run)
                    err = (summary.get("error")
                           if isinstance(summary, dict) else None)
                    self.send_confidence(
                        self.confidence_payload(
                            tap, None, action.get("name"),
                            result=("dispatch-error" if err
                                    else "dead-zone"),
                            error=err, view=view),
                        dry_run=dry_run)
                    return summary
                except Exception as exc:  # keep serving touches
                    LOG.warning("options fallback failed: %s", exc)
                    summary = {"action": "options",
                               "error": str(exc)}
                    self.send_confidence(
                        self.confidence_payload(
                            tap, None, action.get("name"),
                            result="dispatch-error", error=str(exc),
                            view=view),
                        dry_run=dry_run)
                    return summary
            region = next(r for r in candidates
                          if r["id"] == region_id)
            action = region["action"]
            if isinstance(action, dict) \
                    and action.get("name") in COORD_ACTIONS:
                # Tap-supplied coordinates: the region names the action,
                # the tap positions it. Stamped here so _resolve (and the
                # daemon after it) validates the real point; an invalid
                # point is refused below with no byte on the wire.
                action = dict(action, x=tap[0], y=tap[1])
            LOG.info("tap at %d,%d -> region %r -> action %r",
                     tap[0], tap[1], region_id,
                     region["action"].get("name"))
            try:
                summary = self.client.dispatch(
                    action, dry_run=dry_run,
                    panel=(self.config["width"],
                           self.config["height"]))
                # Action first, feedback second: a feedback failure below
                # can never suppress or alter what was just dispatched.
                err = (summary.get("error")
                       if isinstance(summary, dict) else None)
                self.send_confidence(
                    self.confidence_payload(
                        tap, region_id, region["action"].get("name"),
                        result="dispatch-error" if err else "dispatched",
                        error=err, view=view),
                    dry_run=dry_run)
                return summary
            except Exception as exc:  # keep serving touches on HTTP failure
                LOG.warning("action %r failed: %s",
                            region["action"].get("name"), exc)
                summary = {"action": region["action"].get("name"),
                           "error": str(exc)}
                self.send_confidence(
                    self.confidence_payload(
                        tap, region_id, region["action"].get("name"),
                        result="dispatch-error", error=str(exc),
                        view=view),
                    dry_run=dry_run)
                return summary
        return None
