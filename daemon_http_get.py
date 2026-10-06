"""The read-only HTTP surface: GET routes.

Single concept: every GET the daemon serves -- control page, health, version,
state, renderers, snapshot, policy, deploy stamp, the one-time scan relay,
playlist status, the command long-poll endpoints, layout, the touch gate, and
the feedback/feed read-backs. GETs never touch the idle clock.
"""

import json
from urllib.parse import parse_qs, urlsplit

from control_page import CONTROL_PAGE
from daemon_config import APP_VERSION


class GETRoutesMixin:
    """GET route table."""

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(200, CONTROL_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path in ("/health", "/healthz"):
            return self._send(200, {"ok": True})
        auth_error = self._check_token()
        if auth_error is not None:
            return self._send(401, auth_error)
        if path == "/state":
            return self._send(200, self.daemon.state())
        if path == "/version":
            return self._send(200, {"version": APP_VERSION})
        if path == "/renderers":
            return self._send(200, {"renderers": self.daemon.renderer_list()})
        if path == "/snapshot":
            png = self.daemon.snapshot()
            if png is None:
                return self._send(404, {"error": "nothing has been drawn yet"})
            return self._send(200, png, "image/png")
        if path == "/policy":
            return self._send(200, self.daemon.get_policy())
        if path == "/deploy":
            return self._send(200, self.daemon.deploy_info())
        if path.startswith("/r/"):
            # One-time scan relay: consume the token, 302 the scanner to
            # the commit page, and confirm the reload view when it still
            # shows. Observation like any GET: never touches the idle
            # clock. Only the exact /r/<token> shape routes here.
            token = path[len("/r/"):]
            if "/" in token or not token:
                return self._send(404, {"error": "not found"})
            status, payload = self.daemon.handle_relay_scan(token)
            if status == 302:
                body = json.dumps(payload).encode()
                self.send_response(302)
                self.send_header("Location", payload["commit_url"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self._send(status, payload)
        if path == "/playlist":
            return self._send(200, self.daemon.playlist.status())
        if path == "/macbook/mouse":
            # Poller fetch path: ?since=<last acted ts> returns the
            # pending cursor command, or {"command": None} when there
            # is nothing new (or it TTL-expired). Read-only and
            # idempotent -- a retried fetch never double-fires.
            # Optional ?wait=<seconds> holds (bounded, see wait_command)
            # until a tap queues, so the poller wakes on the tap instead
            # of its next tick; absent/zero is today's immediate reply.
            query = parse_qs(urlsplit(self.path).query)
            raw = (query.get("since", [None])[0])
            wait = (query.get("wait", [None])[0])
            return self._send(200, {"command":
                                    self.daemon.wait_command("mouse",
                                                        raw, wait)})
        if path == "/macbook/click":
            # Poller fetch path: ?since=<last acted ts> returns the
            # pending click, or {"command": None} when there is
            # nothing new (or it TTL-expired). Read-only and
            # idempotent -- a retried fetch never double-fires.
            # Optional ?wait=<seconds> holds (bounded, see wait_command)
            # until a tap queues, so the poller wakes on the tap instead
            # of its next tick; absent/zero is today's immediate reply.
            query = parse_qs(urlsplit(self.path).query)
            raw = (query.get("since", [None])[0])
            wait = (query.get("wait", [None])[0])
            return self._send(200, {"command":
                                    self.daemon.wait_command("click",
                                                        raw, wait)})
        if path == "/layout":
            return self._send(200, {"layout": self.daemon.layout_state()})
        if path == "/touch/check":
            # Drawn-vs-live region assertion (see touch_audit.py): 200
            # when the per-view matrix agrees, 409 with the exact
            # differing rects/ids when drifted, 503 when the gate
            # itself is blind (unknown: no heartbeat yet; stale: the
            # heartbeat expired -- an alarm, not a pass). A GET: never
            # touches the idle clock.
            report = self.daemon.touch_check()
            if report.get("ok"):
                return self._send(200, report)
            if report.get("blind"):
                return self._send(503, report)
            return self._send(409, report)
        if path == "/feedback":
            query = parse_qs(urlsplit(self.path).query)
            try:
                return self._send(200, self.daemon.list_feedback(
                    view=(query.get("view", [None])[0]),
                    limit=(query.get("limit", [50])[0])))
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
        if path == "/feedback/summary":
            return self._send(200, self.daemon.feedback_summary())
        if path.startswith("/feedback/"):
            parts = path.split("/")
            if len(parts) == 3 and parts[2]:
                try:
                    return self._send(200, self.daemon.get_feedback(parts[2]))
                except KeyError as exc:
                    return self._send(404, {"error": str(exc)})
            if len(parts) == 4 and parts[2] and parts[3] == "frame":
                try:
                    return self._send(200, self.daemon.feedback_frame(parts[2]),
                                      "image/png")
                except KeyError as exc:
                    return self._send(404, {"error": str(exc)})
                except IOError as exc:
                    return self._send(404, {"error": str(exc),
                                            "frame_present": False})
            return self._send(404, {"error": "not found"})
        if path.startswith("/feed/"):
            parts = path.split("/")
            if len(parts) != 4 or not parts[2] or not parts[3]:
                return self._send(404, {"error": "use /feed/<renderer>/<input>"})
            try:
                return self._send(200, self.daemon.feeds.status(parts[2], parts[3]))
            except KeyError as exc:
                return self._send(404, {"error": str(exc)})
        if path == "/talon/focus":
            # Mac-side poller fetch: the one pending focus command
            # newer than ?since=, else no command. Read-only; the
            # poller tracks what it already acted on.
            # Optional ?wait=<seconds> holds (bounded, see wait_command)
            # until a tap queues, so the poller wakes on the tap instead
            # of its next tick; absent/zero is today's immediate reply.
            query = parse_qs(urlsplit(self.path).query)
            raw = query.get("since", [None])[0]
            wait = query.get("wait", [None])[0]
            return self._send(200, {"command":
                                    self.daemon.wait_command("focus",
                                                        raw, wait)})
        return self._send(404, {"error": "not found"})
