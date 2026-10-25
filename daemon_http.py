"""HTTP handler base: auth, body parsing, responses, and DELETE.

Single concept: the mechanics every request shares. The daemon and the API
token are read through ``self.daemon`` / ``self.api_token``, which the
composition root binds to its own module globals so the documented test hooks
(patching ``displayd.DAEMON`` / ``displayd.API_TOKEN``) keep working. Route
tables live in daemon_http_get.py and daemon_http_post.py.
"""

import hmac
import json
import time

import daemon_stats


class HandlerBaseMixin:
    """Shared HTTP request mechanics."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # BaseHTTPRequestHandler's own lines (send_error, parse failures);
        # the per-request access line is written by handle_one_request.
        daemon_stats.access_logger.debug(fmt, *args)

    def log_request(self, code="-", size="-"):
        pass

    def send_response(self, code, message=None):
        self._status = code
        super().send_response(code, message)

    def handle_one_request(self):
        """One request: serve it, then log one access line and count it.

        Only method, path (no query), status, duration and remote address
        reach the log; headers and bodies never do."""
        self._status = None
        self._error_message = None
        start = time.monotonic()
        try:
            super().handle_one_request()
        except Exception as exc:
            if self._status is None:
                self._status = 500
            self._error_message = type(exc).__name__
            raise
        finally:
            if self._status is not None:
                self._observe(self._status, (time.monotonic() - start) * 1000)

    def _observe(self, status, ms):
        method = getattr(self, "command", None) or "-"
        path = getattr(self, "path", None) or "-"
        daemon_stats.log_access(method, path, status, ms,
                                self.client_address[0])
        stats = getattr(self.daemon, "stats", None)
        if stats is not None:
            stats.record(path, status, self._error_message)

    def _check_token(self):
        """Return None when the request is authorized, else an error body.

        With no API_TOKEN configured the API stays open (historical
        behavior). When it is configured, the bearer token must match in
        constant time, and the compare runs even when the header is absent
        so a missing header does not return measurably faster than a wrong
        one."""
        if not self.api_token:
            return None
        header = self.headers.get("Authorization", "")
        provided = header[7:] if header.startswith("Bearer ") else ""
        if hmac.compare_digest(provided.encode(), self.api_token):
            return None
        return {"error": "unauthorized"}

    def _send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        if code >= 500 and isinstance(payload, dict):
            self._error_message = str(payload.get("error") or "")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body_raw(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None, "empty body"
        try:
            return json.loads(self.rfile.read(length).decode() or "null"), None
        except ValueError:
            return None, "invalid JSON"

    def _body(self):
        payload, _ = self._body_raw()
        return payload if isinstance(payload, dict) else {}

    def do_DELETE(self):
        path = self.path.split("?")[0]
        auth_error = self._check_token()
        if auth_error is not None:
            return self._send(401, auth_error)
        if path == "/layout":
            return self._send(200, self.daemon.clear_layout())
        return self._send(404, {"error": "not found"})
