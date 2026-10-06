"""The reload confirmation view and its one-time tokens.

Single concept: the RELOADED + SHA + QR view -- validation of the requested
sha, issuing the single-scan relay token, the saved base view to return to,
and the tap confirm/dismiss paths. The scan relay's own routes live in
daemon_relay.py.
"""

import secrets
import time

from deploy_reload import (RELOAD_COMMIT_URL_PREFIX, RELOAD_DURATION_MAX,
                           RELOAD_DURATION_MIN, RELOAD_RELAY_PATH,
                           RELOAD_SHA_RE, relay_base_url)
from renderers import reload_highlights as reload_highlights_module
from schema import validate_params


class ReloadMixin:
    """Reload view, tokens, and dismissal."""

    def reload(self, sha=None, duration=None, highlights=None):
        """Show the reload confirmation until a tap dismisses it.

        `sha` must be the full 40-character hexadecimal deployed commit
        SHA; the screen shows RELOADED, the SHA, and a QR code whose payload
        is a panel-served one-time relay URL (relay_base_url() + /r/<token>)
        when the panel binds a tailnet address the captain's phone can
        reach -- otherwise the QR falls back to the commit page and the
        response says tap-only plainly. Scanning the relay 302-redirects
        the scanner to the commit page AND confirms the view (early return
        to whatever was showing); a tap while the reload view shows
        confirms the same way via confirm_reload()/POST /reload/confirm.
        Tokens are single-scan with no time expiry: each dies with its
        view (scan/tap confirm, tap-dismiss, manual navigation, a
        superseding notice, or a newer reload all invalidate it). Raises ValueError
        on bad input (HTTP 400) or KeyError when the reload renderer is not
        installed (404).

        Unlike notify(), there is no return timer: the screen stays on the
        reload view indefinitely. A touchscreen tap (POST /touch/tap via
        dismiss_reload()) returns through the existing return path -- the
        saved base view, or the clock after a fresh restart with no base
        view. A manual /show or /clear cancels the transient outright, and
        reload shares notice's top priority level so the newest of the
        two wins. A legacy `duration` field is still validated when
        supplied but never armed: it is accepted and ignored, and the
        response reports "return_in": None (indefinite). `highlights`
        is an optional bounded commit-message summary (subject + a few
        body lines, extracted Mac-side by deploy.sh where git works);
        it is sanitised and capped here, drawn as plain text only, and a
        missing/malformed value renders the classic view unchanged. It
        never reaches the QR payload."""
        if sha is None or (isinstance(sha, str) and not sha.strip()):
            raise ValueError("sha is required: post the full 40-character "
                             "deployed commit SHA")
        if not isinstance(sha, str):
            raise ValueError("sha must be a string, got %s"
                             % type(sha).__name__)
        if len(sha) != 40:
            raise ValueError("sha must be a full 40-character commit SHA, "
                             "got %d characters" % len(sha))
        if not RELOAD_SHA_RE.match(sha):
            raise ValueError("sha must be hexadecimal (0-9, a-f): "
                             "%r is not a commit SHA" % sha)
        sha = sha.lower()
        if duration is not None:
            # Legacy field: validated for compatibility, ignored for
            # expiry -- the reload view never returns on its own.
            try:
                legacy = float(duration)
            except (TypeError, ValueError):
                raise ValueError("duration must be a number of seconds")
            if not (RELOAD_DURATION_MIN <= legacy <= RELOAD_DURATION_MAX):
                raise ValueError("duration must be within [%d, %d]"
                                 % (RELOAD_DURATION_MIN, RELOAD_DURATION_MAX))
        params = {"sha": sha}
        clean_hl = reload_highlights_module.sanitize(highlights)
        if clean_hl:
            # Plain text beside the code: bounded, no control
            # characters, never URL-shaped into the QR (the renderer
            # enforces the same bounds for /show callers). Absent or
            # malformed input leaves params exactly as before.
            params["highlights"] = clean_hl
        commit_url = RELOAD_COMMIT_URL_PREFIX + sha
        entry = self.renderers.get("reload")
        if not entry or "module" not in entry:
            raise KeyError("reload renderer is not installed")
        now = time.time()
        with self.lock:
            self._prune_reload_tokens_locked(now)
            scan_token = secrets.token_urlsafe(16)
            while scan_token in self.reload_tokens:
                scan_token = secrets.token_urlsafe(16)
            self.reload_tokens[scan_token] = {
                "sha": sha, "commit_url": commit_url,
                # No time expiry: the view stays indefinitely, so the
                # token dies with the view (confirm, tap-dismiss, manual
                # nav, superseding notice, or a newer reload invalidate
                # it) rather than with a duration.
                "expires_at": None,
            }
        base, reachable, reason = relay_base_url()
        if reachable:
            relay_url = base + RELOAD_RELAY_PATH + scan_token
            params["relay_url"] = relay_url
        else:
            # Tap-only fallback: loopback (or otherwise unreachable) bind
            # means the phone could never fetch a relay URL, so never put
            # one in the QR. The renderer draws the commit QR plus an
            # explicit tap-to-confirm note; the response says why.
            # Invalidate the token at once so no dead /r/ URL exists.
            with self.lock:
                self.reload_tokens.pop(scan_token, None)
            scan_token = None
            relay_url = None
        validate_params(params, entry.get("params") or {})
        self.policy.note_api()
        token, superseded = self.policy.begin_transient("reload", None)
        with self.lock:
            # Indefinite: cancel any in-flight return timer and arm none.
            # A stale timer holding an older token is then harmless --
            # end_transient's token check rejects it.
            self._cancel_transient_timer()
            self._wake_if_idle()
            if self.reload_token is not None and self.reload_token != scan_token:
                # A replaced window's token dies with it: one live token
                # per showing view, so a stale QR never redirects to an
                # older commit after a newer reload took the panel.
                self.reload_tokens.pop(self.reload_token, None)
            self.reload_token = scan_token
        self._start_view("reload", params)
        out = {"view": "reload", "params": params,
               "commit_url": commit_url,
               "relay_url": relay_url,
               "relay_reachable": reachable,
               "return_in": None}
        if not reachable:
            out["relay_note"] = reason
        if superseded:
            out["superseded"] = superseded
        return out

    def _prune_reload_tokens_locked(self, now=None):
        """Drop expired reload tokens. Callers hold self.lock.

        Tokens with expires_at None never expire by time -- they die
        with the view -- so pruning only touches time-bounded ones."""
        now = time.time() if now is None else now
        dead = [tok for tok, rec in self.reload_tokens.items()
                if rec.get("expires_at") is not None
                and rec.get("expires_at", 0) <= now]
        for tok in dead:
            self.reload_tokens.pop(tok, None)

    def _return_from_base(self, base):
        """Restore the base view after a confirm (no lock held).

        Mirrors _transient_expired's reload branch: with no explicit base
        view the clock resumes instead of a blank panel. Runs outside
        self.lock because _start_view locks internally (it must -- like
        _transient_expired, callers never hold the daemon lock here)."""
        if base is None:
            try:
                return self._start_view("clock", {})
            except (KeyError, ValueError):
                return self._clear_internal()
        try:
            return self._start_view(base["renderer"], base["params"])
        except (KeyError, ValueError):
            pass

    def reload_confirm_state(self):
        """Pending-confirmation fragment for /state (None when idle)."""
        with self.lock:
            active = self.policy.active
            if active is None or active.get("kind") != "reload":
                return None
            token = self.reload_token
            rec = self.reload_tokens.get(token) if token else None
            now = time.time()
            out = {"pending": True, "via": ["scan", "tap"]}
            if rec is not None and rec.get("expires_at") is not None:
                out["expires_in"] = round(
                    max(0.0, rec["expires_at"] - now), 1)
            else:
                # Indefinite window (or tap-only fallback with no token):
                # no countdown, the view waits for a scan or a tap.
                out["expires_in"] = None
                if rec is None:
                    out["tap_only"] = True
            return out

    def dismiss_reload(self):
        """Dismiss an active reload transient after a touchscreen tap.

        Takes the same return path as the old expiry timer: the saved base
        view resumes, or the clock after a fresh restart with no base
        view. Dismissing anything but an active reload -- a notice, an
        attention pull, a plain view, or nothing at all -- is a harmless
        no-op reporting dismissed False, so repeated taps are safe. A tap
        is operator presence, so a successful dismissal notes API activity."""
        base, ok = self.policy.dismiss_transient("reload")
        if not ok:
            return {"dismissed": False, "view": self.current}
        self.policy.note_api()
        with self.lock:
            self._cancel_transient_timer()
            self._wake_if_idle()
            if self.reload_token is not None:
                # The window is over: its one-time token dies with the
                # view (late scans get 410, never a stale confirm).
                self.reload_tokens.pop(self.reload_token, None)
                self.reload_token = None
        try:
            if base is None:
                try:
                    self._start_view("clock", {})
                except (KeyError, ValueError):
                    self._clear_internal()
            else:
                self._start_view(base["renderer"], base["params"])
        except (KeyError, ValueError):
            pass
        return {"dismissed": True, "view": self.current}
