"""The emulated bridge poller for the tap-latency harness.

Single concept: the production bridge's poll loop, reproduced so stage C
(queue -> poller fetch) can be timed -- one daemon thread holding a
``?since=`` GET at the production cadence (long-poll when asked), recording
every command the daemon hands back, and re-parking at once after acting so
a back-to-back tap wakes instead of riding out the tick sleep.
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tap_latency_fixtures import http_get


def start_poller(base, path, key, interval, wait, stop, seen):
    """Launch the emulated poller for `path`; returns the started Thread.

    `seen[key]` is (perf_counter, command) for the newest command the
    daemon handed back, which the caller pops to time stage C.
    """

    def poller():
        since = [0.0]
        while not stop.is_set():
            t0 = time.monotonic()
            acted = False
            try:
                _, doc = http_get(
                    base, "%s?since=%s%s" % (
                        path, since[0],
                        ("&wait=%s" % wait) if wait else ""),
                    timeout=10.0)
                cmd = (doc.get("command") or {})
                if isinstance(cmd, dict) and cmd.get("ts", 0) > 0:
                    since[0] = max(since[0], float(cmd["ts"]))
                    seen[key] = (time.perf_counter(), dict(cmd))
                    acted = True
            except Exception:
                pass
            if acted:
                time.sleep(0.05)
            else:
                time.sleep(max(0.05, interval
                               - (time.monotonic() - t0)))

    thread = threading.Thread(target=poller, name="tap-latency-poller",
                              daemon=True)
    thread.start()
    return thread
