"""Request observability: GET /stats counters and the access log.

Headless daemon over real HTTP, like test_surfaces_http.py.
DISPLAYD_FAKE_FB=1 selects the in-memory framebuffer double.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import logging
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import daemon_stats  # noqa: E402
import displayd  # noqa: E402
from test_surfaces_http import SurfaceHttpTestCase  # noqa: E402

TOKEN = "s3cret-token-value"


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class StatsCase(SurfaceHttpTestCase):
    def setUp(self):
        super().setUp()
        self.log = Capture()
        logger = daemon_stats.access_logger
        old = (logger.level, logger.propagate)
        logger.addHandler(self.log)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        self.addCleanup(lambda: ([logger.removeHandler(h) for h in
                                  list(logger.handlers)
                                  if getattr(h, "_displayd", False)],
                                 logger.removeHandler(self.log),
                                 logger.setLevel(old[0]),
                                 setattr(logger, "propagate", old[1])))

    def stats(self, **kw):
        code, out = self.call("GET", "/stats", **kw)
        self.assertEqual(code, 200, out)
        return out


class TestStats(StatsCase):
    def test_shape_after_200_401_404(self):
        patch = mock.patch.object(displayd, "API_TOKEN", TOKEN.encode())
        patch.start()
        self.addCleanup(patch.stop)
        self.assertEqual(self.call("GET", "/state", token=TOKEN)[0], 200)
        self.assertEqual(self.call("GET", "/state")[0], 401)
        self.assertEqual(self.call("GET", "/nope", token=TOKEN)[0], 404)
        out = self.stats(token=TOKEN)
        self.assertEqual(out["version"], displayd.APP_VERSION)
        self.assertRegex(out["started_at"], r"^\d{4}-\d\d-\d\dT")
        self.assertGreaterEqual(out["uptime_s"], 0)
        self.assertEqual(out["requests"]["total"], 3)
        self.assertEqual(out["requests"]["by_status"],
                         {"2xx": 1, "4xx": 2, "5xx": 0})
        self.assertEqual(out["errors"], {"count": 0, "last_error": None})
        self.assertEqual(out["renderers"], len(self.daemon.renderers))
        self.assertEqual(set(out["feeds"]), {"cold", "warm", "stale"})

    def test_stats_requires_token(self):
        patch = mock.patch.object(displayd, "API_TOKEN", TOKEN.encode())
        patch.start()
        self.addCleanup(patch.stop)
        self.assertEqual(self.call("GET", "/stats")[0], 401)

    def test_health_unauthenticated_and_unchanged(self):
        patch = mock.patch.object(displayd, "API_TOKEN", TOKEN.encode())
        patch.start()
        self.addCleanup(patch.stop)
        self.assertEqual(self.call("GET", "/health"), (200, {"ok": True}))

    def test_route_families_ignore_query_and_depth(self):
        self.call("GET", "/surfaces")
        self.call("GET", "/surfaces/phone")
        self.call("GET", "/state?x=1")
        out = self.stats()
        routes = out["requests"]["by_route"]
        self.assertEqual((routes["surfaces"], routes["state"]), (2, 1))

    def test_5xx_counts_as_error_with_last_error(self):
        stats = self.daemon.stats
        stats.record("/show?secret=1", 500, "boom")
        out = self.stats()
        self.assertEqual(out["errors"]["count"], 1)
        last = out["errors"]["last_error"]
        self.assertEqual((last["path"], last["status"], last["message"]),
                         ("/show", 500, "boom"))
        self.assertEqual(set(last), {"time", "path", "status", "message"})
        self.assertEqual(out["requests"]["by_status"]["5xx"], 1)

    def test_route_families_are_bounded(self):
        stats = daemon_stats.RequestStats()
        for i in range(daemon_stats.MAX_FAMILIES + 10):
            stats.record("/scan%d" % i, 404)
        routes = stats.snapshot()["requests"]["by_route"]
        self.assertEqual(len(routes), daemon_stats.MAX_FAMILIES + 1)
        self.assertEqual(routes["other"], 10)


class TestAccessLog(StatsCase):
    LINE = re.compile(r"^method=GET path=/state status=200 ms=\d+\.\d "
                      r"remote=127\.0\.0\.1$")

    def test_line_format_drops_query(self):
        self.call("GET", "/state?token=abc&x=1")
        self.assertEqual(len(self.log.lines), 1, self.log.lines)
        self.assertRegex(self.log.lines[0], self.LINE)

    def test_token_and_bodies_never_logged(self):
        patch = mock.patch.object(displayd, "API_TOKEN", TOKEN.encode())
        patch.start()
        self.addCleanup(patch.stop)
        self.call("POST", "/show", {"renderer": "text",
                                    "params": {"text": "PRIVATE-BODY"}},
                  token=TOKEN)
        self.call("GET", "/state", token="wrong-" + TOKEN)
        text = "\n".join(self.log.lines)
        self.assertEqual(len(self.log.lines), 2)
        for secret in (TOKEN, "Bearer", "Authorization", "PRIVATE-BODY"):
            self.assertNotIn(secret, text)
        self.assertIn("status=401", text)

    def test_relay_token_in_path_is_masked(self):
        self.call("GET", "/r/one-time-secret")
        self.assertIn("path=/r/*", self.log.lines[0])
        self.assertNotIn("one-time-secret", self.log.lines[0])

    def test_level_gate(self):
        daemon_stats.configure_logging({"DISPLAYD_LOG_LEVEL": "WARNING"})
        self.addCleanup(daemon_stats.configure_logging, {})
        self.call("GET", "/health")
        self.assertEqual(self.log.lines, [])
        daemon_stats.configure_logging({"DISPLAYD_LOG_LEVEL": "info"})
        self.call("GET", "/health")
        self.assertEqual(len(self.log.lines), 1)

    def test_bad_level_falls_back_to_info(self):
        self.assertEqual(
            daemon_stats.configure_logging({"DISPLAYD_LOG_LEVEL": "loud"}),
            logging.INFO)
        self.addCleanup(daemon_stats.configure_logging, {})


if __name__ == "__main__":
    unittest.main()
