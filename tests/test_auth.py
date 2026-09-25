"""Bearer-token auth tests for the displayd HTTP API.

No framebuffer needed: the handler validates tokens before touching the
daemon, and we stub ``DAEMON`` with a stand-in whose methods would be
called on an authorized request. Verifies the auth *gate*, not the
endpoints themselves.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import unittest
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib import request
from urllib.error import HTTPError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd

HOST = "127.0.0.1"
PORT = 0  # ephemeral: picked by ThreadingHTTPServer
TOKEN = "s3cret-tok3n"


@staticmethod
def _read_body(exc):
    """Read and close the response so Python stops warning about it."""
    body = exc.read()
    exc.close()
    return body


class StubDaemon:
    def __init__(self):
        self.calls = []

    def state(self):
        self.calls.append("state")
        return {"stub": True}

    def clear_layout(self):
        self.calls.append("clear_layout")
        return {"ok": True}

    def get_config(self):
        return {"notifications": {"enabled": False}}


def open_url(url, token=None):
    """GET url with an optional bearer token, returning (status, body)."""
    req = request.Request(url)
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except HTTPError as exc:
        return exc.code, _read_body(exc)


class AuthGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        displayd.API_TOKEN = TOKEN.encode()
        cls.old_daemon = displayd.DAEMON
        displayd.DAEMON = StubDaemon()
        cls.server = ThreadingHTTPServer((HOST, PORT), displayd.Handler)
        cls.server.daemon_threads = True
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        displayd.API_TOKEN = b""
        displayd.DAEMON = cls.old_daemon

    def base(self, path):
        return "http://%s:%d%s" % (HOST, self.port, path)

    def setUp(self):
        displayd.DAEMON.calls = []

    # -- token enforced ----------------------------------------------------

    def test_requires_token_when_configured(self):
        status, body = open_url(self.base("/state"))
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "unauthorized"})
        self.assertEqual(displayd.DAEMON.calls, [],
                         "unauthorized request must not reach the daemon")

    def test_rejects_wrong_token(self):
        status, _ = open_url(self.base("/state"), token="nope")
        self.assertEqual(status, 401)

    def test_accepts_correct_token(self):
        status, body = open_url(self.base("/state"), token=TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"stub": True})
        self.assertEqual(displayd.DAEMON.calls, ["state"])

    # -- health stays exempt ----------------------------------------------

    def test_health_exempt_when_configured(self):
        status, body = open_url(self.base("/health"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    # -- method coverage ---------------------------------------------------

    def test_post_and_delete_are_gated(self):
        for method, path in (("POST", "/notify"), ("DELETE", "/layout")):
            with self.subTest(method=method):
                req = request.Request(
                    self.base(path), method=method,
                    data=b"{}" if method == "POST" else None)
                try:
                    request.urlopen(req, timeout=10)
                except HTTPError as exc:
                    status = exc.code
                    _read_body(exc)
                else:
                    status = 200
                self.assertEqual(status, 401, "%s %s not gated" % (method, path))
        self.assertEqual(displayd.DAEMON.calls, [],
                         "blocked requests must not reach the daemon")


class LoopbackUnchangedTest(unittest.TestCase):
    """With no token configured the API stays open (historical behavior)."""

    @classmethod
    def setUpClass(cls):
        displayd.API_TOKEN = b""
        cls.old_daemon = displayd.DAEMON
        displayd.DAEMON = StubDaemon()
        cls.server = ThreadingHTTPServer((HOST, PORT), displayd.Handler)
        cls.server.daemon_threads = True
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        displayd.DAEMON = cls.old_daemon

    def base(self, path):
        return "http://%s:%d%s" % (HOST, self.port, path)

    def test_open_without_token(self):
        status, body = open_url(self.base("/state"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"stub": True})


if __name__ == "__main__":
    unittest.main()