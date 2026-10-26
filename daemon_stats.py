"""Request observability: the access log and the in-process counters.

Single concept: what the HTTP layer saw. ``RequestStats`` is the thread-safe
counter set behind ``GET /stats`` (reset on restart, nothing persisted);
``log_access`` writes one structured line per request on the
``displayd.access`` logger. Only method, path without its query string,
status, duration and remote address are ever recorded -- never headers
(so never the API token) and never request or response bodies. The one
path that embeds a secret, the scan relay ``/r/<token>``, is masked.
"""

import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

LOG_LEVEL_ENV = "DISPLAYD_LOG_LEVEL"
MAX_FAMILIES = 64        # bound on distinct route families (404 scanners)
MAX_MESSAGE = 200

access_logger = logging.getLogger("displayd.access")


def configure_logging(environ=None):
    """Point the access logger at stderr at ``DISPLAYD_LOG_LEVEL`` (default
    INFO; an unknown name falls back to INFO). Idempotent."""
    env = os.environ if environ is None else environ
    name = (env.get(LOG_LEVEL_ENV) or "INFO").strip().upper()
    level = logging.getLevelName(name)
    access_logger.setLevel(level if isinstance(level, int) else logging.INFO)
    if not any(getattr(h, "_displayd", False) for h in access_logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s"))
        handler._displayd = True
        access_logger.addHandler(handler)
        access_logger.propagate = False
    return access_logger.level


def safe_path(raw):
    """Path for logging: query/fragment dropped, relay token masked,
    anything that could forge a log line (control chars, spaces) removed."""
    path = (raw or "-").split("?")[0].split("#")[0]
    if path.startswith("/r/"):
        path = "/r/*"
    return "".join(c for c in path if c.isprintable() and c != " ")[:200] or "-"


def family(path):
    """Route family: the first path segment (``/surfaces/phone`` ->
    ``surfaces``; ``/`` -> ``/``)."""
    segment = path.strip("/").split("/")[0]
    return segment or "/"


def log_access(method, path, status, ms, remote):
    access_logger.info("method=%s path=%s status=%d ms=%.1f remote=%s",
                       method, safe_path(path), status, ms, remote)


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(
        timespec="seconds")


class RequestStats:
    """Thread-safe request counters (ThreadingHTTPServer runs one thread
    per connection)."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self.started_at = clock()
        self._total = 0
        self._classes = {"2xx": 0, "4xx": 0, "5xx": 0}
        self._families = {}
        self._errors = 0
        self._last_error = None

    def record(self, path, status, message=None):
        path = safe_path(path)
        status_class = "%dxx" % (status // 100)
        with self._lock:
            self._total += 1
            self._classes[status_class] = self._classes.get(status_class, 0) + 1
            fam = family(path)
            if fam not in self._families and len(self._families) >= MAX_FAMILIES:
                fam = "other"
            self._families[fam] = self._families.get(fam, 0) + 1
            if status >= 500:
                self._errors += 1
                self._last_error = {
                    "time": _iso(self._clock()), "path": path,
                    "status": status,
                    "message": (message or "HTTP %d" % status)[:MAX_MESSAGE]}

    def snapshot(self):
        now = self._clock()
        with self._lock:
            return {
                "started_at": _iso(self.started_at),
                "uptime_s": round(now - self.started_at, 1),
                "requests": {"total": self._total,
                             "by_status": dict(self._classes),
                             "by_route": dict(self._families)},
                "errors": {"count": self._errors,
                           "last_error": (dict(self._last_error)
                                          if self._last_error else None)},
            }
