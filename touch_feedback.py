"""Confidence-feedback publishing for resolved taps.

Single concept: the best-effort tap report that feeds the touch_confidence
viewer. ``confidence_payload`` builds the payload (pixel + normalized
coordinates, region, action, result) and ``send_confidence`` POSTs it --
always after action dispatch, never able to suppress or alter a tap's
action.
"""

import logging
import time

LOG = logging.getLogger("displayd-touch")


class FeedbackMixin:
    """Confidence-feedback payload + best-effort publish."""

    def confidence_payload(self, tap, region_id, action_name,
                             result=None, error=None, view=None):
        """Build the touch_confidence tap feed payload for a resolved tap.

        tap is an (x, y) display-pixel pair; x_norm/y_norm are the same
        point as 0..1 fractions so the confidence view (or any consumer)
        can place it without knowing the panel size. Pure: no I/O."""
        x, y = tap
        width, height = self.config["width"], self.config["height"]
        payload = {
            "x": int(x),
            "y": int(y),
            "x_norm": round(x / float(width - 1), 4) if width > 1 else 0.0,
            "y_norm": round(y / float(height - 1), 4) if height > 1 else 0.0,
            "hit": region_id is not None,
            "view": view,
            "ts": time.time(),
        }
        if region_id is not None:
            payload["region"] = region_id
        if action_name is not None:
            payload["action"] = action_name
        if result is not None:
            payload["result"] = str(result)
        if error is not None:
            payload["error"] = str(error)
        return payload

    def send_confidence(self, payload, dry_run=False):
        """POST one tap payload to the confidence feed. Best-effort: any
        failure (or a disabled switch, or dry-run) is logged and swallowed
        -- feedback must never suppress or alter action dispatch."""
        if not (self.confidence or {}).get("enabled"):
            return None
        if dry_run:
            LOG.info("confidence feedback (dry-run, not sent): %r", payload)
            return None
        path = "/feed/%s/%s" % (self.confidence["renderer"],
                                   self.confidence["input"])
        try:
            return self.client.post(path, payload)
        except Exception as exc:  # keep serving touches on HTTP failure
            LOG.warning("confidence feedback to %s failed "
                        "(best-effort, action already dispatched): %s",
                        path, exc)
            return None
