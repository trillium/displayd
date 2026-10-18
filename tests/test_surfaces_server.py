"""Sidecar server (/surfaces) refusal and auth paths over HTTP.

test_surfaces_http.py covers the happy paths and the gate on four routes.
This file covers what it leaves out: the bearer gate on every POST
sub-route, malformed/empty bodies, unknown sub-routes, bad poll numbers,
and which requests do (and do not) count as operator activity.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import http.client
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd  # noqa: E402
from test_surfaces_http import SurfaceHttpTestCase, sidecar  # noqa: E402


class SidecarServerCase(SurfaceHttpTestCase):
    def setUp(self):
        super().setUp()
        self.call("POST", "/surfaces", sidecar("phone"))

    def raw(self, method, path, body=None, headers=None):
        """Send body bytes verbatim (call() always json-encodes)."""
        host, port = self.base[len("http://"):].split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=10)
        self.addCleanup(conn.close)
        hdrs = dict(headers or {})
        if body is not None:
            hdrs["Content-Length"] = str(len(body))
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, resp.read().decode()


class TestAuthGate(SidecarServerCase):
    ROUTES = (
        ("POST", "/surfaces/phone", {"name": "x"}),
        ("POST", "/surfaces/phone/heartbeat", {}),
        ("POST", "/surfaces/phone/content", {"content": {"type": "text", "text": "a"}}),
        ("POST", "/surfaces/content", {"target": "phone",
                                       "content": {"type": "text", "text": "a"}}),
        ("POST", "/surfaces/phone/touch", {"interaction": "tap"}),
        ("POST", "/surfaces/phone/columns", {"columns": []}),
        ("POST", "/surfaces/phone/columns/c/action", {"action": "a"}),
        ("GET", "/surfaces/phone", None),
        ("GET", "/surfaces/phone/content", None),
        ("GET", "/surfaces/phone/columns", None),
        ("DELETE", "/surfaces/phone", None),
    )

    def test_every_route_refuses_without_a_valid_token(self):
        with mock.patch.object(displayd, "API_TOKEN", b"s3cret"):
            for method, path, body in self.ROUTES:
                self.assertEqual(self.call(method, path, body)[0], 401, path)
                self.assertEqual(self.call(method, path, body, token="bad")[0],
                                 401, path)
            # nothing above reached a handler: the surface is intact
            self.assertEqual(self.call("GET", "/surfaces/phone",
                                       token="s3cret")[0], 200)

    def test_refusal_does_not_leak_or_mutate(self):
        with mock.patch.object(displayd, "API_TOKEN", b"s3cret"):
            code, out = self.call("DELETE", "/surfaces/phone")
            self.assertEqual((code, out), (401, {"error": "unauthorized"}))
            self.call("POST", "/surfaces/phone/content",
                      {"content": {"type": "text", "text": "x"}})
            self.assertEqual(self.call("GET", "/surfaces/phone/content",
                                       token="s3cret")[1]["contents"], {})

    def test_only_the_bearer_scheme_is_accepted(self):
        with mock.patch.object(displayd, "API_TOKEN", b"s3cret"):
            for header in ("Basic s3cret", "s3cret", "bearer s3cret", "Bearer"):
                self.assertEqual(self.raw("GET", "/surfaces",
                                          headers={"Authorization": header})[0],
                                 401, header)
            self.assertEqual(self.raw("GET", "/surfaces", headers={
                "Authorization": "Bearer s3cret"})[0], 200)

    def test_open_when_no_token_is_configured(self):
        with mock.patch.object(displayd, "API_TOKEN", b""):
            self.assertEqual(self.call("GET", "/surfaces")[0], 200)


class TestBadRequests(SidecarServerCase):
    def test_malformed_and_empty_bodies_are_400(self):
        for path in ("/surfaces", "/surfaces/phone", "/surfaces/content",
                     "/surfaces/phone/content", "/surfaces/phone/touch",
                     "/surfaces/phone/columns"):
            code, body = self.raw("POST", path, b"{not json")
            self.assertEqual(code, 400, path)
            self.assertIn("invalid JSON", body)
            code, body = self.raw("POST", path)
            self.assertEqual(code, 400, path)
            self.assertIn("empty body", body)

    def test_non_object_bodies_are_400(self):
        for path in ("/surfaces/content", "/surfaces/phone/content",
                     "/surfaces/phone/touch", "/surfaces/phone/columns"):
            self.assertEqual(self.call("POST", path, [1])[0], 400, path)
            self.assertEqual(self.call("POST", path, "text")[0], 400, path)

    def test_bad_columns_payload_is_400_and_keeps_the_old_columns(self):
        self.assertEqual(self.call("POST", "/surfaces/phone/columns",
                                   {"columns": "nope"})[0], 400)
        self.assertEqual(self.call("GET", "/surfaces/phone/columns")[1]["columns"], [])

    def test_unknown_subroutes_are_404(self):
        for method, path in (("POST", "/surfaces/phone/bogus"),
                             ("GET", "/surfaces/phone/bogus"),
                             ("GET", "/surfaces/phone/a/b"),
                             ("DELETE", "/surfaces/phone/content"),
                             ("DELETE", "/surfaces")):
            self.assertEqual(self.call(method, path, {} if method == "POST" else None)[0],
                             404, (method, path))

    def test_unknown_surface_is_404_on_every_sub_resource(self):
        for method, path, body in (("GET", "/surfaces/gone/content", None),
                                   ("GET", "/surfaces/gone/events", None),
                                   ("GET", "/surfaces/gone/columns", None),
                                   ("POST", "/surfaces/gone/touch", {"interaction": "tap"}),
                                   ("POST", "/surfaces/gone/columns", {"columns": []})):
            self.assertEqual(self.call(method, path, body)[0], 404, path)

    def test_non_numeric_poll_parameters_are_400(self):
        for path in ("/surfaces/phone/content?since=x", "/surfaces/phone/content?wait=x",
                     "/surfaces/phone/events?since=x", "/surfaces/phone/events?wait=x"):
            code, out = self.call("GET", path)
            self.assertEqual(code, 400, path)
            self.assertIn("expected a number", out["error"])

    def test_unsupported_touch_interaction_is_409(self):
        code, out = self.call("POST", "/surfaces/phone/touch", {"interaction": "pinch"})
        self.assertEqual(code, 409, out)
        self.assertEqual(self.call("GET", "/surfaces/phone/events")[1]["events"], [])

    def test_unknown_column_action_is_404(self):
        code, out = self.call("POST", "/surfaces/phone/columns/c/action",
                              {"action": "ack"})
        self.assertEqual(code, 404, out)


class TestActivityClock(SidecarServerCase):
    def mark(self):
        self.daemon.policy.last_api = -1.0
        return -1.0

    def test_reads_and_heartbeats_never_count_as_activity(self):
        mark = self.mark()
        self.call("GET", "/surfaces")
        self.call("GET", "/surfaces/phone")
        self.call("GET", "/surfaces/phone/content")
        self.call("GET", "/surfaces/phone/events")
        self.call("POST", "/surfaces/phone/heartbeat", {})
        self.assertEqual(self.daemon.policy.last_api, mark)

    def test_mutating_sends_count_as_activity(self):
        for method, path, body in (
                ("POST", "/surfaces", sidecar("other")),
                ("POST", "/surfaces/phone", {"name": "renamed"}),
                ("POST", "/surfaces/phone/content",
                 {"content": {"type": "text", "text": "a"}}),
                ("POST", "/surfaces/phone/touch", {"interaction": "tap", "x": 1, "y": 2}),
                ("POST", "/surfaces/phone/columns", {"columns": []}),
                ("DELETE", "/surfaces/other", None)):
            mark = self.mark()
            self.assertLess(self.call(method, path, body)[0], 300, path)
            self.assertNotEqual(self.daemon.policy.last_api, mark, path)

    def test_unauthorized_requests_do_not_count_as_activity(self):
        mark = self.mark()
        with mock.patch.object(displayd, "API_TOKEN", b"s3cret"):
            self.call("POST", "/surfaces/phone/content",
                      {"content": {"type": "text", "text": "a"}})
        self.assertEqual(self.daemon.policy.last_api, mark)


if __name__ == "__main__":
    unittest.main()
