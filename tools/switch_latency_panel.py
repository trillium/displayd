"""A headless daemon on loopback with its framebuffer write watched.

Single concept: the instrument the switch-latency gate reads. ``raw`` is the
last step of every present (compose-then-swap writes once), so wrapping it
timestamps the pixel, not the draw. Nothing here knows about budgets.
"""

import os
import threading
import time
from http.server import ThreadingHTTPServer

import displayd
from tap_latency_fixtures import http_post

QUIET_S = 0.08
BASELINE = ("solid", {"color": "#101820"})


class Panel(object):
    """A headless daemon on loopback, with the framebuffer write watched."""

    def __init__(self, tmp):
        # Set here, not only at import: another test may have cleared it.
        os.environ["DISPLAYD_FAKE_FB"] = "1"
        self.daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(tmp, "policy.json"),
            feedback_path=os.path.join(tmp, "feedback.jsonl"))
        displayd.DAEMON = self.daemon
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), displayd.Handler)
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.painted_at = None
        self.armed = False
        self.last_image = None
        self._raw = self.daemon.fb.raw
        self._present = self.daemon.fb.present

        def watched_present(img):
            if self.armed:
                self.last_image = img
            return self._present(img)

        def watched(data):
            if self.armed and data != self.daemon.fb.last_frame:
                self.painted_at = time.perf_counter()
            return self._raw(data)

        self.daemon.fb.present = watched_present
        self.daemon.fb.raw = watched

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5.0)

    def post(self, path, body):
        _ms, payload = http_post(self.base, path, body)
        return payload

    def arm(self):
        self.painted_at = None
        self.last_image = None
        self.armed = True

    def settle_baseline(self, timeout):
        """Put a known-different view up, wait for its paint, then let the
        outgoing view finish talking. A view thread that presents once more
        after it was stopped would otherwise answer the NEXT measurement
        and make the target look instant."""
        self.post("/show", {"renderer": BASELINE[0], "params": BASELINE[1]})
        deadline = time.perf_counter() + timeout
        while self.painted_at is None and time.perf_counter() < deadline:
            time.sleep(0.002)
        time.sleep(QUIET_S)
        return self.daemon.fb.last_frame


def engine_status(daemon):
    """The html view's own answer to "can you draw?", never raising."""
    module = (daemon.renderers.get("html") or {}).get("module")
    if module is None:
        return {"ok": False, "library": None, "error": "no html view"}
    try:
        return module.runtime_status()
    except Exception as err:  # a status probe that can fail is not a status
        return {"ok": False, "library": None, "error": str(err)}
