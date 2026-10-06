"""The reload QR relay: one-time scan -> commit page -> confirm.

Single concept: the ``/r/<token>`` relay a scanned QR hits. A valid token is
consumed once, 302s the scanner to the commit page, and confirms the showing
reload view; everything else is a miss. No tracking beyond the confirm.
"""

import copy

from deploy_reload import RELOAD_TOKEN_RE


class RelayMixin:
    """One-time scan relay and reload confirm."""

    def confirm_reload(self, source="tap", token=None):
        """Confirm the showing reload view: return to the base view early.

        `source` is "tap" (POST /reload/confirm, touch.py reload_confirm)
        or "scan" (GET /r/<token>). Scan confirms only with the live
        one-time token; tap confirms whatever reload is showing. Both are
        view-gated: with no active reload transient the call is a 409-style
        miss ({confirmed: False}), never a view change. Idempotent: the
        second confirm of the same window misses the same way. Returns a
        JSON-safe dict; raises nothing."""
        source = str(source or "tap").lower()
        if source not in ("tap", "scan"):
            return {"confirmed": False, "reason": "unknown source %r"
                    % (source,)}
        with self.lock:
            active = self.policy.active
            if (active is None or active.get("kind") != "reload"):
                return {"confirmed": False,
                        "reason": "no-reload-active"}
            if source == "scan":
                if not token or not isinstance(token, str):
                    return {"confirmed": False,
                            "reason": "token-required"}
                self._prune_reload_tokens_locked()
                rec = self.reload_tokens.get(token)
                if rec is None:
                    return {"confirmed": False,
                            "reason": "unknown-or-expired-token"}
                if token != self.reload_token:
                    return {"confirmed": False,
                            "reason": "stale-token"}
                # Single-scan: consume first, then return the panel.
                self.reload_tokens.pop(token, None)
                self.reload_token = None
            else:
                if self.reload_token is not None:
                    self.reload_tokens.pop(self.reload_token, None)
                    self.reload_token = None
            ok = self.policy.end_transient(
                "reload", active.get("token"))
            if not ok[1]:
                return {"confirmed": False,
                        "reason": "already-confirmed"}
            self._cancel_transient_timer()
            self.policy.note_api()
            base = copy.deepcopy(self.policy.base)
        # Outside self.lock: _start_view locks internally (same shape as
        # _transient_expired -- holding both would deadlock).
        self._return_from_base(base)
        return {"confirmed": True, "via": source}

    def handle_relay_scan(self, token):
        """One scan of GET /r/<token>: consume + redirect + confirm.

        Returns (status, payload): (302, commit_url) on the single valid
        scan -- the handler 302-redirects there and the panel has been
        returned early when it was still showing; (410, {...}) when the
        token was already consumed or expired; (404, {...}) when unknown
        or malformed. The token dies with the view (timeout, manual
        navigation, superseding notice, or a newer reload all invalidate
        it), so a scan after the return gets 410 and confirms nothing."""
        if not token or not isinstance(token, str) \
                or not RELOAD_TOKEN_RE.match(token):
            return 404, {"error": "unknown confirm token"}
        with self.lock:
            self._prune_reload_tokens_locked()
            rec = self.reload_tokens.get(token)
            if rec is None:
                return 410, {"error": "token expired or already scanned: "
                                        "single-scan, valid only while the "
                                        "reload view shows"}
            commit_url = rec["commit_url"]
            live = (self.policy.active is not None
                    and self.policy.active.get("kind") == "reload"
                    and token == self.reload_token)
            # Single-scan: consume before confirming so a double-fetch
            # cannot confirm twice.
            self.reload_tokens.pop(token, None)
            base = None
            confirmed = False
            if live:
                active_token = self.policy.active.get("token")
                self.reload_token = None
                ok = self.policy.end_transient("reload", active_token)
                if ok[1]:
                    self._cancel_transient_timer()
                    self.policy.note_api()
                    base = copy.deepcopy(self.policy.base)
                    confirmed = True
        # Outside self.lock: _start_view locks internally (same shape as
        # _transient_expired -- holding both would deadlock).
        if confirmed:
            self._return_from_base(base)
        return 302, {"commit_url": commit_url,
                     "confirmed": confirmed}
